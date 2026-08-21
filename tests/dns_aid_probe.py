"""Resolve this domain's DNS for AI Discovery records and report what is actually served.

Not a pytest module (the filename keeps it out of collection): it queries the public DNS,
so it asserts nothing about a build — it tells you what a validator standing outside the
zone can see right now.

    python tests/dns_aid_probe.py technocore.chat

Queries over DNS-over-HTTPS rather than the resolver library, because that is the transport
DNS-AID validators use and it is the one whose answers include the `AD` flag verbatim. It
also sidesteps a local dig too old to render SVCB presentation format: DiG 9.10 prints an
unrelated RRset for `-t HTTPS` rather than admitting it does not know the type.

Two names are expected to be *absent*, and the probe says so rather than staying quiet.
`_a2a._agents` and `_mcp._agents` are omitted on purpose: the HTTP origin implements neither
protocol, and the MCP wrapper is a stdio distribution with no hosted endpoint for a service
binding to point at. Finding records there means somebody published a claim the origin cannot
answer, which is why it is reported as a finding rather than as a bonus.

A miss right after publishing is usually cache, not a failed write: the SOA carries a 1800s
negative TTL, so a resolver that answered NODATA before publication keeps doing so. Confirm
against the authoritative servers before believing this probe:

    dig -t TYPE64 _index._agents.<domain> @cloe.ns.cloudflare.com
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# Cloudflare first, Google as the fallback, matching what the readiness scanners do. A
# resolver-level failure on one is not evidence about the zone, so the probe tries both
# before calling a name missing.
RESOLVERS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)

SVCB, TXT, DS = 64, 16, 43

# What each name is for, and whether it is meant to exist. The absent ones are listed so a
# false claim published later shows up as a finding instead of going unnoticed.
NAMES = (
    ("_index._agents", SVCB, True, "well-known entry point"),
    ("_index._agents", TXT, True, "index hint (not draft-defined)"),
    ("_chat._agents", SVCB, True, "the service itself"),
    ("_a2a._agents", SVCB, False, "omitted — no A2A at this origin"),
    ("_mcp._agents", SVCB, False, "omitted — the MCP wrapper is stdio, not hosted"),
)


def query(name: str, rrtype: int) -> dict | None:
    """One DoH lookup with `do=1`, so the answer carries the resolver's DNSSEC verdict."""
    params = urllib.parse.urlencode({"name": name, "type": rrtype, "do": "1"})
    for base in RESOLVERS:
        # Both URLs are literals in RESOLVERS; nothing here takes a caller-supplied scheme.
        req = urllib.request.Request(f"{base}?{params}", headers={"Accept": "application/dns-json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue
    return None


def answers(payload: dict | None, rrtype: int) -> list[str]:
    """The record data for the type asked about; CNAMEs and other chaff are dropped."""
    if not payload:
        return []
    return [a["data"] for a in payload.get("Answer", []) if a.get("type") == rrtype]


def main(domain: str) -> int:
    print(f"DNS-AID under _agents.{domain}\n")
    problems = 0

    for label, rrtype, expected, note in NAMES:
        fqdn = f"{label}.{domain}"
        payload = query(fqdn, rrtype)
        found = answers(payload, rrtype)
        kind = {SVCB: "SVCB", TXT: "TXT"}[rrtype]

        if payload is None:
            verdict, problems = "RESOLVER FAILED", problems + 1
        elif bool(found) == expected:
            verdict = "ok"
        else:
            verdict, problems = ("MISSING" if expected else "UNEXPECTED"), problems + 1

        print(f"  {kind:5} {label:14} {verdict:15} ({note})")
        for record in found:
            print(f"        {record}")

    # The chain of trust, which is a registrar action and not a zone edit: the zone can be
    # signed — RRSIGs present — while the parent holds no DS, and then every answer above
    # is unauthenticated however well-formed it looks.
    ds = query(domain, DS)
    signed = bool(answers(ds, DS))
    authenticated = bool(ds and ds.get("AD"))
    print(f"\n  DNSSEC  DS at parent: {'yes' if signed else 'NO'}", end="")
    print(f"   AD flag: {'yes' if authenticated else 'NO'}")
    if not (signed and authenticated):
        print("        chain incomplete — the records resolve but nothing authenticates them")
        problems += 1

    print(f"\n{'all as documented' if not problems else f'{problems} finding(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "technocore.chat"))
