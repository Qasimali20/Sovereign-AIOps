# SOC playbook: authentication attacks

## Brute force and password spraying
Many failures for one account from one source = password guessing (T1110.001). One or few attempts against
many accounts from one source = password spraying (T1110.003). Containment for lab sources: block the source IP
in the lab firewall. On real nodes: escalate with the source, accounts and time window; suggest fail2ban or
sshd MaxAuthTries and confirming key-only authentication.

## Success after failures
A successful login after repeated failures from the same source suggests a guessed credential (T1078 Valid
Accounts). Treat the account as compromised until shown otherwise: in the lab disable the account; on real
nodes escalate for credential rotation and session review. Failures from an internal admin host followed by a
key-based success is usually an operator: say what would confirm it.

## Sudo failures
Repeated sudo authentication failures by one user suggest privilege-escalation attempts or a mistyped password.
Correlate with the user's login source and time; in the lab the containment is disabling the lab account.
