"""customer-support — agency package (peer-to-peer team, Rule 1).

YAML-FIRST: no per-agent modules live here. Every business rule is in
the configuration's prompts; insights / SLA / cross-channel state is
read from your support desk + CRM via n8n connector agents. The only
Python is _shared/audit_logger.py (WORM sink), registered in main.py.
"""
