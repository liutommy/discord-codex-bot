#!/bin/sh
# Runs inside a throwaway container on one network. Space-separated lists:
#   MUST_FAIL host:port that must be unreachable     DNS_MUST_FAIL names that must not resolve
#   MUST_PASS host:port that must be reachable       DNS_MUST_PASS names that must resolve
# Exit 1 = something that must be blocked got through (a leak); 2 = only a MUST_PASS check failed
# (no internet / DNS: cannot tell); 0 = all as expected. One line per check.
leak=0; down=0
tcp() {  # tcp <host:port> <expect: open|closed>
    host=${1%:*}; port=${1##*:}
    if nc -z -w3 "$host" "$port" >/dev/null 2>&1; then got=open; else got=closed; fi
    if [ "$got" = "$2" ]; then mark=ok; elif [ "$2" = closed ]; then mark=LEAK; leak=1; else mark=DOWN; down=1; fi
    echo "$mark tcp $1 expect=$2 got=$got"
}
dns() {  # dns <name> <expect: resolves|nxdomain>
    ans=$(dig +short +time=2 +tries=1 "$1" A 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
    if [ -n "$ans" ]; then got=resolves; else got=nxdomain; fi
    if [ "$got" = "$2" ]; then mark=ok; elif [ "$2" = nxdomain ]; then mark=LEAK; leak=1; else mark=DOWN; down=1; fi
    echo "$mark dns $1 expect=$2 got=$got${ans:+ ($ans)}"
}
for t in $MUST_FAIL; do tcp "$t" closed; done
for n in $DNS_MUST_FAIL; do dns "$n" nxdomain; done
for t in $MUST_PASS; do tcp "$t" open; done
for n in $DNS_MUST_PASS; do dns "$n" resolves; done
[ $leak = 1 ] && exit 1
[ $down = 1 ] && exit 2
exit 0
