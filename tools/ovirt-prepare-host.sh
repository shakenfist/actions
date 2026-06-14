#!/bin/bash
# Prepare an oVirt node for its role in a deployment.
#
# This script runs on an oVirt target node (not the CI runner / controller).
# It accepts a single role argument so the same script serves both the
# all-in-one single-node deployments (4.4/4.5, kerbside CI) and the split
# engine + hypervisor two-node deployments (4.3):
#
#   engine      Engine-side prep only:
#                 1. Restart ovirt-engine and wait for it to be healthy
#                 2. Install the oVirt Python SDK (used by the smoke test,
#                    which runs on the engine)
#   hypervisor  Hypervisor-side prep only:
#                 1. Enable root SSH login (so the engine can deploy VDSM)
#                 2. Verify KVM is available for nested virtualization
#                 3. Create the local storage directory
#                 4. Install netaddr (the host-deploy Ansible role needs it)
#   all         Both of the above, for a single node that is engine AND
#               hypervisor (the default when no argument is given, which is
#               what every existing single-node caller passes).
#
# Splitting engine vs hypervisor matters because on a two-node deployment
# registering the engine's *own* machine as a host would tear down the
# engine API's network mid-deploy; a dedicated hypervisor avoids that.

set -xe
export PS4='=======================\n+ '

ROLE="${1:-all}"
case "${ROLE}" in
    engine|hypervisor|all) ;;
    *) echo "usage: $0 [engine|hypervisor|all]" >&2; exit 1 ;;
esac

# ----------------------------------------------------------------------
# Engine-side prep.
# ----------------------------------------------------------------------
if [ "${ROLE}" = "engine" ] || [ "${ROLE}" = "all" ]; then
    # Restart ovirt-engine so SSO works (engine-setup says to do this).
    # Then wait for the engine's health endpoint to respond, which
    # indicates the JBoss/WildFly application server has fully started
    # and deployed the engine.ear application. We use the health
    # endpoint rather than the SSO token endpoint because Keycloak
    # (oVirt 4.5) uses a different auth flow that the internal SSO
    # endpoint doesn't support.
    sudo systemctl restart ovirt-engine
    echo 'Waiting for oVirt engine to become ready...'
    engine_ready=0
    for i in $(seq 1 60); do
        resp=$(curl -sk \
            https://localhost/ovirt-engine/services/health \
            2>/dev/null) || true
        if echo "$resp" | grep -q 'DB Up'; then
            echo 'oVirt engine is ready'
            engine_ready=1
            break
        fi
        echo "  Attempt $i/60: engine not ready, waiting 10s..."
        sleep 10
    done
    if [ $engine_ready -ne 1 ]; then
        echo 'ERROR: oVirt engine never became ready. Dumping diagnostics...'
        sudo systemctl status ovirt-engine || true
        echo '--- Last 50 lines of engine.log ---'
        sudo tail -50 /var/log/ovirt-engine/engine.log 2>/dev/null || true
        echo '--- Last 50 lines of server.log ---'
        sudo tail -50 /var/log/ovirt-engine/server.log 2>/dev/null || true
        exit 1
    fi

    # Install the oVirt Python SDK (used by the smoke test, which runs on
    # the engine). oVirt 4.3 / el7 predates the python3 packaging: it ships
    # only the python2 SDK (python-ovirt-engine-sdk4, a compiled C
    # extension). el8/el9 use the python3 name. Pick by RHEL major. On el7
    # the smoke test actually runs under rh-python38 with a pip-built SDK
    # (ovirt-install-sdk-py38.sh) rather than this python2 one, but
    # installing it is harmless and keeps single-node el7 behaviour intact.
    if [ "$(rpm -E %rhel)" = "7" ]; then
        sudo dnf install -y python-ovirt-engine-sdk4
    else
        sudo dnf install -y python3-ovirt-engine-sdk4
    fi
fi

# ----------------------------------------------------------------------
# Hypervisor-side prep.
# ----------------------------------------------------------------------
if [ "${ROLE}" = "hypervisor" ] || [ "${ROLE}" = "all" ]; then
    # Enable root SSH login so oVirt engine can deploy VDSM.
    # Use a drop-in file if sshd_config.d exists (Rocky 9+), or
    # modify sshd_config directly (Rocky 8). Either way, ensure
    # root login and password auth are enabled.
    echo 'root:foobar' | sudo chpasswd
    sudo passwd -u root 2>/dev/null || true
    if [ -d /etc/ssh/sshd_config.d ]; then
        printf 'PermitRootLogin yes\nPasswordAuthentication yes\n' \
            | sudo tee /etc/ssh/sshd_config.d/00-ovirt-ci.conf > /dev/null
    else
        sudo sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
        sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
    fi
    sudo systemctl restart sshd
    echo '--- SSH config verification ---'
    sudo sshd -T 2>/dev/null | grep -iE 'permitrootlogin|passwordauthentication'

    # Verify KVM is available for nested virtualization
    echo '--- KVM availability ---'
    ls -la /dev/kvm 2>/dev/null || echo 'WARNING: /dev/kvm not found'
    lsmod | grep kvm || echo 'WARNING: no kvm modules loaded'
    cat /proc/cpuinfo | grep -c vmx || cat /proc/cpuinfo | grep -c svm \
        || echo 'WARNING: no hardware virt extensions'

    # Create local storage directory (uid 36 = vdsm on RHEL-based)
    sudo mkdir -p /srv/ovirt-storage
    sudo chown 36:36 /srv/ovirt-storage

    # Install netaddr: the host-deploy Ansible role's ipaddr filter (OVN
    # configuration task) needs it, and it runs on this host. el7 ships the
    # python2 name, el8/el9 the python3 name.
    if [ "$(rpm -E %rhel)" = "7" ]; then
        sudo dnf install -y python2-netaddr
    else
        sudo dnf install -y python3-netaddr
    fi
fi
