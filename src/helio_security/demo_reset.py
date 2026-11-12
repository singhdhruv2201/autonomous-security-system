"""Explicit destructive reset of this Compose project's demo volumes."""
import argparse
import subprocess


def reset(confirmation, runner=subprocess.run):
    if confirmation != 'RESET DEMO':
        raise ValueError('Type RESET DEMO to erase current demo data; credentials and host usage ledgers are retained')
    runner(['docker','compose','down','-v'],check=True)
    runner(['docker','compose','up','--build','-d','--wait',
            'simulator','crm','workbench','outside','neo4j','security-api','dashboard'],check=True)


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    confirmation=input('Erase all current demo cases, graph, CRM, workbench and directory data? Type RESET DEMO: ')
    try:
        reset(confirmation)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print('Fresh demo is ready. The paid model/Slack worker remains stopped; start it explicitly when ready.')


if __name__=='__main__': main()
