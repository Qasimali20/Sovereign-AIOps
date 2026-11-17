"""Local MITRE ATT&CK catalogue (subset relevant to this fleet's telemetry). Local on purpose:
the platform is sovereign and makes no external threat-intel calls."""
TECHNIQUES: dict[str, dict] = {
    "T1110.001": {"name": "Brute Force: Password Guessing", "tactic": "Credential Access"},
    "T1110.003": {"name": "Brute Force: Password Spraying", "tactic": "Credential Access"},
    "T1078": {"name": "Valid Accounts", "tactic": "Initial Access / Persistence / Privilege Escalation"},
    "T1021.004": {"name": "Remote Services: SSH", "tactic": "Lateral Movement"},
    "T1548.003": {"name": "Abuse Elevation Control Mechanism: Sudo and Sudo Caching", "tactic": "Privilege Escalation"},
    "T1059.004": {"name": "Command and Scripting Interpreter: Unix Shell", "tactic": "Execution"},
    "T1105": {"name": "Ingress Tool Transfer", "tactic": "Command and Control"},
    "T1071.001": {"name": "Application Layer Protocol: Web Protocols", "tactic": "Command and Control"},
    "T1571": {"name": "Non-Standard Port", "tactic": "Command and Control"},
    "T1068": {"name": "Exploitation for Privilege Escalation", "tactic": "Privilege Escalation"},
    "T1190": {"name": "Exploit Public-Facing Application", "tactic": "Initial Access"},
    "T1195.002": {"name": "Supply Chain Compromise: Compromise Software Supply Chain", "tactic": "Initial Access"},
}


def describe(tid: str) -> dict:
    t = TECHNIQUES.get(tid)
    return {"id": tid, **t} if t else {"id": tid, "name": "unknown", "tactic": "unknown"}
