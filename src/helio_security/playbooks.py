"""Playbook registry and implementations for actions against external systems.

Each playbook exposes:
- id: unique identifier (e.g. crm.pause_principal)
- reversible: bool
- run(target, **kwargs): execute containment or fix
- undo(target, before, **kwargs): reverse containment
- verify(target, **kwargs): check observable state
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
import httpx


class PlaybookError(Exception):
    """Raised when a playbook execution or verification fails."""
    pass


class PausePrincipalPlaybook:
    id = "crm.pause_principal"
    reversible = True

    async def _read_account(self, crm_url: str, observer_token: str, target: str) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{crm_url.rstrip('/')}/accounts",
                headers={"Authorization": f"Bearer {observer_token}"},
            )
            if resp.status_code != 200:
                raise PlaybookError(f"Failed to read accounts: HTTP {resp.status_code}")
            accounts = resp.json()
            for acc in accounts:
                if acc["id"] == target:
                    return acc
            raise PlaybookError(f"Principal {target} not found in CRM accounts")

    async def run(
        self,
        target: str,
        crm_url: str,
        action_token: str,
        observer_token: str,
        force_version: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        acc = await self._read_account(crm_url, observer_token, target)
        if acc.get("kind") != "agent":
            raise PlaybookError(f"Target {target} must be an agent, got {acc.get('kind')}")

        before = {
            "principal_status": acc.get("principal_status", "active"),
            "access_version": acc.get("access_version", 1),
        }
        version = force_version if force_version is not None else acc.get("access_version", 1)

        url = f"{crm_url.rstrip('/')}/principals/{target}/access"
        payload = {"expected_version": version, "principal_status": "disabled"}
        headers = {"Authorization": f"Bearer {action_token}"}

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.patch(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    after = resp.json()
                    return {"status": "executed", "before": before, "after": after}
                elif resp.status_code == 409:
                    raise PlaybookError("Access state changed; stale version")
                elif resp.status_code < 500:
                    raise PlaybookError(f"Failed to pause principal: HTTP {resp.status_code} {resp.text}")
                # 5xx falls through to reconciliation
        except (httpx.TimeoutException, httpx.NetworkError):
            pass

        # Reconcile on timeout or 5xx
        re_read = await self._read_account(crm_url, observer_token, target)
        if re_read.get("principal_status") == "disabled":
            return {"status": "executed", "before": before, "after": re_read}
        raise PlaybookError("Dispatch failed or timed out; state unknown")

    async def undo(
        self,
        target: str,
        before: dict,
        crm_url: str,
        action_token: str,
        observer_token: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        acc = await self._read_account(crm_url, observer_token, target)
        if acc.get("kind") != "agent":
            raise PlaybookError(f"Target {target} must be an agent, got {acc.get('kind')}")

        target_status = before.get("principal_status", "active")
        version = acc.get("access_version", 1)

        url = f"{crm_url.rstrip('/')}/principals/{target}/access"
        payload = {"expected_version": version, "principal_status": target_status}
        headers = {"Authorization": f"Bearer {action_token}"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise PlaybookError(f"Failed to undo pause: HTTP {resp.status_code} {resp.text}")
            return {"status": "undone", "result": resp.json()}

    async def verify(
        self,
        target: str,
        crm_url: str,
        observer_token: str,
        expected_state: str = "disabled",
        **kwargs: Any,
    ) -> bool:
        acc = await self._read_account(crm_url, observer_token, target)
        return acc.get("principal_status") == expected_state


class RevokeGrantPlaybook:
    id = "crm.revoke_grant"
    reversible = False

    async def _read_grants_and_accounts(self, crm_url: str, observer_token: str):
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {observer_token}"}
            g_resp = await client.get(f"{crm_url.rstrip('/')}/grants", headers=headers)
            a_resp = await client.get(f"{crm_url.rstrip('/')}/accounts", headers=headers)
            if g_resp.status_code != 200 or a_resp.status_code != 200:
                raise PlaybookError("Failed to read grants or accounts from CRM")
            return g_resp.json(), a_resp.json()

    async def run(
        self,
        target: str,
        crm_url: str,
        action_token: str,
        observer_token: str,
        force_version: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        grants, accounts = await self._read_grants_and_accounts(crm_url, observer_token)
        grant = next((g for g in grants if g["id"] == target), None)
        if not grant:
            raise PlaybookError(f"Grant {target} not found")

        principal = next((a for a in accounts if a["id"] == grant["principal_id"]), None)
        if not principal or principal.get("kind") != "agent":
            raise PlaybookError(f"Target grant {target} does not belong to an agent principal")

        before = dict(grant)
        version = force_version if force_version is not None else grant.get("version", 1)

        url = f"{crm_url.rstrip('/')}/grants/{target}/revoke"
        payload = {"expected_version": version}
        headers = {"Authorization": f"Bearer {action_token}"}

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    return {"status": "executed", "before": before, "after": resp.json()}
                elif resp.status_code == 409:
                    raise PlaybookError("Grant state changed; stale version")
                elif resp.status_code < 500:
                    raise PlaybookError(f"Failed to revoke grant: HTTP {resp.status_code} {resp.text}")
        except (httpx.TimeoutException, httpx.NetworkError):
            pass

        # Reconcile on timeout or 5xx
        re_grants, _ = await self._read_grants_and_accounts(crm_url, observer_token)
        re_grant = next((g for g in re_grants if g["id"] == target), None)
        if re_grant and re_grant.get("status") == "revoked":
            return {"status": "executed", "before": before, "after": re_grant}
        raise PlaybookError("Dispatch failed or timed out; state unknown")

    async def undo(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise PlaybookError("crm.revoke_grant is not reversible")

    async def verify(
        self,
        target: str,
        crm_url: str,
        observer_token: str,
        expected_state: str = "revoked",
        **kwargs: Any,
    ) -> bool:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {observer_token}"}
            resp = await client.get(f"{crm_url.rstrip('/')}/grants", headers=headers)
            if resp.status_code != 200:
                return False
            grant = next((g for g in resp.json() if g["id"] == target), None)
            return bool(grant and grant.get("status") == expected_state)


REGISTRY = {
    "crm.pause_principal": PausePrincipalPlaybook(),
    "crm.revoke_grant": RevokeGrantPlaybook(),
}


def get_playbook(playbook_id: str):
    try:
        return REGISTRY[playbook_id]
    except KeyError:
        raise PlaybookError(f"Unknown playbook: {playbook_id}")
