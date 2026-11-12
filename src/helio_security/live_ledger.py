"""Durable reservations for explicitly authorized live tests (not billing records)."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import time
import uuid

PRICING_TABLE = {
    # Short-context standard rates. The investigator bounds input below 100k.
    "claude-haiku-5-5": {"input_per_m": 0.10, "output_per_m": 0.50},
    "claude-3-5-haiku-20241022": {"input_per_m": 0.80, "output_per_m": 4.00},
}


class BudgetExceededError(RuntimeError):
    """Allowance exhausted, or accounting cannot establish remaining allowance."""


def _tokens(value):
    if type(value) is not int or value < 0:
        raise BudgetExceededError("Token counts must be non-negative integers")
    return value


def calculate_cost(tokens_in, tokens_out, model="claude-haiku-5-5"):
    if model not in PRICING_TABLE:
        raise BudgetExceededError("Unknown pricing for model; failing closed")
    rates = PRICING_TABLE[model]
    # A single long-context request uses the higher Haiku tier.
    multiplier = 5 if model == "claude-haiku-5-5" and tokens_in > 100_000 else 1
    return multiplier * (_tokens(tokens_in) * rates["input_per_m"] +
                         _tokens(tokens_out) * rates["output_per_m"]) / 1_000_000


class LiveLedger:
    def __init__(self, ledger_path="data/live_test_ledger.json", max_spend_usd=1.0, max_requests=10):
        self.path = Path(ledger_path)
        self.max_spend_usd = max_spend_usd
        self.max_requests = max_requests
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if not self.path.exists():
                self._save({"schema_version": 2, "max_spend_usd": max_spend_usd,
                            "max_requests": max_requests, "total_requests": 0,
                            "total_input_tokens": 0, "total_output_tokens": 0,
                            "total_estimated_cost_usd": 0.0,
                            "pending_reservations": {}, "runs": []})

    @contextmanager
    def _lock(self):
        # Lock a separate stable inode: replacing the JSON must not release it.
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            for key in ("total_requests", "total_input_tokens", "total_output_tokens", "max_requests"):
                _tokens(data[key])
            for key in ("total_estimated_cost_usd", "max_spend_usd"):
                if not math.isfinite(data[key]) or data[key] < 0:
                    raise ValueError()
            if not isinstance(data["runs"], list) or not isinstance(data.get("pending_reservations", {}), dict):
                raise ValueError()
            for reservation in data.get("pending_reservations", {}).values():
                cost = reservation["estimated_cost_usd"]
                if not math.isfinite(cost) or cost < 0:
                    raise ValueError()
            return data
        except Exception:
            raise BudgetExceededError("Corrupt or missing ledger; allowance cannot be established") from None

    def get_data(self):
        with self._lock():
            return self._load()

    def _save(self, data):
        temporary = self.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w") as output:
                json.dump(data, output, indent=2, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
            descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _check(self, data, est_in, est_out, model):
        if data.get("schema_version") != 2:
            raise BudgetExceededError("Legacy ledger requires usage reconciliation before more live calls")
        pending = data["pending_reservations"]
        if data["total_requests"] + len(pending) >= min(self.max_requests, data["max_requests"]):
            raise BudgetExceededError("Request count ceiling reached")
        cost = calculate_cost(est_in, est_out, model)
        committed = data["total_estimated_cost_usd"] + sum(r["estimated_cost_usd"] for r in pending.values())
        if committed + cost > min(self.max_spend_usd, data["max_spend_usd"]):
            raise BudgetExceededError("Spending limit would be exceeded")
        return cost

    def can_execute(self, est_in=4000, est_out=1000, model="claude-haiku-5-5"):
        try:
            with self._lock():
                self._check(self._load(), est_in, est_out, model)
            return True
        except BudgetExceededError:
            return False

    def check_and_reserve(self, est_in=4000, est_out=1000, model="claude-haiku-5-5"):
        with self._lock():
            data = self._load()
            cost = self._check(data, est_in, est_out, model)
            identifier = str(uuid.uuid4())
            data["pending_reservations"][identifier] = {
                "timestamp": time.time(), "model": model, "estimated_cost_usd": cost,
                "est_in": est_in, "est_out": est_out,
            }
            self._save(data)
            return identifier

    def record_request(self, model, tokens_in=None, tokens_out=None, status="success",
                       error=None, duration_seconds=None, reservation_id=None):
        with self._lock():
            data = self._load()
            if reservation_id:
                previous = next((r for r in data["runs"] if r.get("reservation_id") == reservation_id), None)
                if previous:
                    return previous
                reservation = data["pending_reservations"].get(reservation_id)
                if not reservation or reservation["model"] != model:
                    raise BudgetExceededError("Unknown or mismatched reservation")
            else:
                reservation = None
            known = tokens_in is not None and tokens_out is not None
            if known:
                cost = calculate_cost(tokens_in, tokens_out, model)
            elif reservation:
                # Charge the full reserved ceiling, while leaving usage unknown.
                cost = reservation["estimated_cost_usd"]
                tokens_in = tokens_out = None
            else:
                raise BudgetExceededError("Unknown usage requires an explicit reservation")
            if reservation_id:
                del data["pending_reservations"][reservation_id]
            data["total_requests"] += 1
            data["total_input_tokens"] += tokens_in or 0
            data["total_output_tokens"] += tokens_out or 0
            data["total_estimated_cost_usd"] += cost
            entry = {"timestamp": time.time(), "model": model, "reservation_id": reservation_id,
                     "input_tokens": tokens_in, "output_tokens": tokens_out,
                     "estimated_cost_usd": cost, "status": status, "usage_known": known}
            if error:
                entry["error"] = "request_failed"  # never provider response text
            if duration_seconds is not None:
                entry["duration_seconds"] = duration_seconds
            data["runs"].append(entry)
            self._save(data)
            return entry


def configured_ledger():
    """Use one durable allowance for an explicitly configured live worker."""
    path = os.getenv('AWASI_LIVE_LEDGER')
    if not path:
        return None
    spend = float(os.getenv('AWASI_LIVE_MAX_SPEND_USD', '1.0'))
    requests = int(os.getenv('AWASI_LIVE_MAX_REQUESTS', '10'))
    if not math.isfinite(spend) or spend <= 0 or requests <= 0:
        raise ValueError('Live allowance must be positive and finite')
    return LiveLedger(path, max_spend_usd=spend, max_requests=requests)
