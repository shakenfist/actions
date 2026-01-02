#!/bin/bash

failures=0

echo
echo "Running log checks for branch ${1} and job ${2}."
echo
for target in /var/log/syslog /var/log/syslog.1; do
    if [ ! -e ${target} ]; then
        echo "MISSING: ${target} does not exist."
    else
        echo "LENGTH: ${target} is "$(cat ${target} | wc -l)" lines long."
    fi
done

if [ -e /var/log/syslog ]; then
    etcd_conns_a=$(grep -c "Building new etcd connection" /var/log/syslog || true)
    sigterms_a=$(grep -c "Sent SIGTERM to " /var/log/syslog || true)
else
    etcd_conns_a=0
    sigterms_a=0
fi

if [ -e /var/log/syslog.1 ]; then
    etcd_conns_b=$(grep -c "Building new etcd connection" /var/log/syslog.1 || true)
    sigterms_b=$(grep -c "Sent SIGTERM to " /var/log/syslog.1 || true)
else
    etcd_conns_b=0
    sigterms_b=0
fi

echo
echo "etcd connections: ${etcd_conns_a} from syslog, ${etcd_conn_b} from syslog.1"
etcd_conns=$(( ${etcd_conns_a} + ${etcd_conns_b} ))
echo "This CI run created ${etcd_conns} etcd connections."
if [ ${etcd_conns} -gt 5000 ]; then
    echo "FAILURE: Too many etcd clients!"
    failures=$(( ${failures} + 1 ))
fi

echo
echo "sigterms: ${sigterms_a} from syslog, ${sigterms_b} from syslog.1"
sigterms=$(( ${sigterms_a} + ${sigterms_b} ))
echo "This CI run sent ${sigterms} SIGTERM signals while shutting down."
if [ ${sigterms} -gt 50 ]; then
    echo "FAILURE: Too many SIGTERMs sent!"
    failures=$(( ${failures} + 1 ))
fi

# NOTE(mikal): online upgrades are forbidden in these fresh install
# tests.
echo
FORBIDDEN=("Traceback (most recent call last):"
           "ERROR gunicorn"
           "Extra vxlan present"
           "Fork support is only compatible with the epoll1 and poll polling strategies"
           "not using configured address"
           "Dumping thread traces"
           "because it is leased to"
           "Received a GOAWAY with error code ENHANCE_YOUR_CALM"
           "ConnectionFailedError"
           "invalid JWT in Authorization header"
           "Libvirt Error: XML error"
           "cluster wide cleanup daemon is deleting this IPAM as leaked"
           "Cleaning up leaked vxlan"
           "invalid salt"
           "unable to execute QEMU command"
           "bad argument type for built-in operation"
           "libvirt: QEMU Driver error : unsupported configuration"
           "unsupported configuration: disk type of 'vdc' does not support ejectable media")

if [ $(echo "${1}" | grep -c "v0.7" || true) -lt 1 ]; then
    echo "INFO: Including forbidden strings for v0.8 onwards."
    FORBIDDEN+=('apparmor="DENIED"')
    FORBIDDEN+=("Ignoring malformed cache entry")
    FORBIDDEN+=("WORKER TIMEOUT")
    FORBIDDEN+=("Repeated failures to add address to device")
    FORBIDDEN+=("Lock held by missing process on this node")
    FORBIDDEN+=("Cannot record event, no configured server")
    FORBIDDEN+=("Cannot communicate with etcd, no configured server")
    FORBIDDEN+=("Recreating not okay network on hypervisor")
    FORBIDDEN+=("Failed to change thread name")

    # grpc specific
    FORBIDDEN+=("segfault")
    FORBIDDEN+=("*** Check failure stack trace: ***")
    FORBIDDEN+=("Unhandled gRPC call failure")

    # systemd errors
    FORBIDDEN+=("State 'stop-sigterm' timed out. Killing.")
    FORBIDDEN+=("Main process exited, code=exited")
fi

if [ $(echo "${2}" | grep -c "upgrade" || true) -lt 1 ]; then
    echo "INFO: Including forbidden strings for non-upgrade jobs."
    FORBIDDEN+=("online upgrade")
fi

IFS=""
for forbid in ${FORBIDDEN[*]}
do
    echo "    Check for >>${forbid}<< in logs."

    for target in /var/log/syslog /var/log/syslog.1; do
        if [ ! -e ${target} ]; then
            continue
        fi

        count=$(grep -c -i "$forbid" "${target}" || true)
        if [ ${count} -gt 0 ]
        then
            echo "FAILURE: Forbidden string found in ${target} ${count} times."
            failures=$(( $failures + 1))
        fi
    done
done

# Forbidden once stable, which we currently define as after the first 1,000
# lines of the syslog file.
FORBIDDEN_ONCE_STABLE=("ERROR sf"
                       "Failed to send event with gRPC"
                       "Unknown server error while sending multi event with gRPC"
                       "not committing online upgrade"
                       "Cluster not yet stable"
                       "StatusCode.UNAVAILABLE"
                       "Failed with result 'exit-code'."
                       "API query for node told node not ready"
                       "Not processing queues as dependencies are unhealthy")
IFS=""
for forbid in ${FORBIDDEN_ONCE_STABLE[*]}
do
    echo "    Check for >>${forbid}<< in stable logs."

    count=$(tail -n +1000 /var/log/syslog | grep -c -i "$forbid" || true)
    if [ ${count} -gt 0 ]
    then
        echo "FAILURE: Forbidden once stable string found ${count} times."
        failures=$(( $failures + 1))
    fi
done

echo
if [ $failures -gt 0 ]; then
    echo "...${failures} failures detected."
    exit 1
fi

# Just a warning for now, likely to get promoted to a failure later...
failures=0
WARNING=("Waiting to acquire lock"
         "Transaction failure"
         "Lock refreshers should not be used under gunicorn")

IFS=""
for forbid in ${WARNING[*]}
do
    echo "    Check for >>${forbid}<< in logs."
    count=$(grep -c -i "$forbid" /var/log/syslog || true)
    if [ ${count} -gt 0 ]
    then
        echo "WARNING: Undesirable string found in logs ${count} times."
        failures=$(( $failures + 1))
    fi
done

echo
if [ $failures -gt 0 ]; then
    echo "...${failures} warnings detected."
fi
