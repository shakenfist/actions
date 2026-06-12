#!/bin/bash
# Restore SPICE on an EL9 oVirt host via the ligenix enterprise-qemu-spice
# COPR.
#
# RHEL/Rocky 9 removed SPICE and the qxl video model from qemu-kvm, so a
# stock el9 oVirt host can only offer VNC consoles: a SPICE VM fails to
# start with "domain configuration does not support video model 'qxl'".
# The ligenix/enterprise-qemu-spice COPR ships a qemu-kvm rebuilt with the
# qxl device and spice module, plus the qemu-kvm-ui-spice backend and the
# spice-server library el9 dropped entirely. Stock el9 libvirt still
# accepts <graphics type='spice'> (the COPR does not rebuild libvirtd), so
# swapping qemu is enough to bring SPICE back.
#
# oVirt itself is host-blind about the video model -- the engine emits qxl
# purely from the VM's SPICE display setting, with no host-capability
# check -- so once qemu supports qxl again the engine's existing SPICE
# config just works, with no oVirt-side change.
#
# This must run BEFORE the host is registered (registration is what pulls
# in VDSM and qemu-kvm), so the COPR qemu is in place from the start
# rather than swapped in underneath a running stack.
#
# Usage: ovirt-enable-el9-spice.sh

set -xe
export PS4='=======================\n+ '

# Drop the COPR repo directly rather than via "dnf copr enable": the copr
# plugin maps Rocky 9 to a "rhel-9" chroot this project does not build,
# whereas the RHEL9-compatible "epel-9" chroot does exist.
#
# includepkgs restricts the repo to the qemu/SPICE family so a later
# "dnf update" during host-deploy cannot pull the COPR's many unrelated
# rebuilds (glib2, glusterfs, avahi, ...) and destabilise the system.
#
# priority=1 is essential, not cosmetic. qemu-kvm wins on epoch (18 vs
# RHEL's 17), but its seabios-bin/seavgabios-bin dependencies do NOT have
# an epoch bump and the COPR builds them at a LOWER release than stock
# (e.g. seavgabios-bin 1.16.3-4.el9_spice vs stock 1.16.3-5.el9). On
# version alone dnf would keep the stock seavgabios-bin, which omits
# vgabios-qxl.bin -- so a qxl VM then dies at start with "failed to find
# romfile vgabios-qxl.bin". priority=1 makes dnf prefer the COPR build of
# every included package regardless of version, so the rom-carrying
# seavgabios-bin wins.
#
# skip_if_unavailable keeps a future build working from the local RPM
# mirror (which captures these RPMs) even if the COPR itself goes away.
sudo tee /etc/yum.repos.d/ligenix-enterprise-qemu-spice.repo > /dev/null <<'EOF'
[copr:copr.fedorainfracloud.org:ligenix:enterprise-qemu-spice]
name=Copr repo for enterprise-qemu-spice owned by ligenix
baseurl=https://download.copr.fedorainfracloud.org/results/ligenix/enterprise-qemu-spice/epel-9-$basearch/
type=rpm-md
skip_if_unavailable=True
gpgcheck=1
gpgkey=https://download.copr.fedorainfracloud.org/results/ligenix/enterprise-qemu-spice/pubkey.gpg
repo_gpgcheck=0
enabled=1
priority=1
includepkgs=qemu-* spice-* libcacard* usbredir* seabios* seavgabios* virglrenderer*
EOF

# Pull the SPICE-enabled qemu now. The COPR qemu-kvm carries epoch 18,
# which supersedes RHEL's epoch-17 qemu-kvm regardless of version, so dnf
# selects the COPR build automatically. qemu-kvm-ui-spice provides the
# SPICE display backend el9 ships nothing for. Installing both before
# host-deploy means VDSM's qemu-kvm dependency is already satisfied by the
# COPR build.
sudo dnf install -y qemu-kvm qemu-kvm-ui-spice

# Force the COPR seabios/seavgabios builds even if a stock (higher-release)
# one was already installed: "dnf install" above will not downgrade an
# already-present package, but distro-sync honours the repo priority and
# switches them to the COPR builds. This is what brings in vgabios-qxl.bin.
sudo dnf distro-sync -y seabios-bin seavgabios-bin

# Self-verify: without vgabios-qxl.bin a SPICE VM fails to start. Catch it
# here, where the message is actionable, rather than later at VM boot.
if ! rpm -ql seavgabios-bin | grep -q vgabios-qxl.bin; then
    echo 'ERROR: seavgabios-bin has no vgabios-qxl.bin -- SPICE VMs will' \
         'not start. The COPR seavgabios-bin did not win; check priority=1.' >&2
    exit 1
fi

echo '--- SPICE qemu in place (provenance should be the *_spice COPR build) ---'
rpm -q --qf '%{NAME} %{EPOCH}:%{VERSION}-%{RELEASE} (%{VENDOR})\n' \
    qemu-kvm qemu-kvm-ui-spice seavgabios-bin seabios-bin
echo "vgabios-qxl.bin: $(rpm -ql seavgabios-bin | grep vgabios-qxl.bin)"
