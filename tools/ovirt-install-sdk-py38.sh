#!/bin/bash
# Install the python3 oVirt SDK into rh-python38 on an el7 engine box.
#
# oVirt 4.3 / el7 ships only the python2 SDK (python-ovirt-engine-sdk4),
# but the generic smoke test (start-test-target.py) is python3-only. el7
# also has no usable system python3 with the SDK, so we build
# ovirt-engine-sdk-python (and its pycurl dependency) for the rh-python38
# 3.8 interpreter the rest of the deployment already uses.
#
# The build deps (gcc, libxml2-devel, libcurl-devel, openssl-devel,
# rh-python38-python-devel) come from the local mirror; only the SDK and
# pycurl sdists are pulled from PyPI here -- PyPI is alive (unlike the EOL
# yum mirrors), and this is a validation step, not the deployment itself.
#
# The SDK version is pinned to match the 4.3 engine (the el7 RPM is
# 4.3.4) to minimise API drift. The SDK builds a libxml2-backed C
# extension and depends on pycurl, hence the headers above.

set -xe
export PS4='=======================\n+ '

PY=/opt/rh/rh-python38/root/usr/bin/python3.8
SDK_VERSION="${OVIRT_SDK_VERSION:-4.3.4}"

# Install the C build toolchain + headers from the local mirror (in
# mirror-only mode it is the only enabled repo). The SDK's C extension
# needs libxml2-devel + gcc + the rh-python38 3.8 headers; its pycurl
# dependency needs curl-config (libcurl-devel) and, since el7's libcurl
# uses the NSS SSL backend, nss-devel. These were captured into the mirror
# by build-ovirt-43-mirror.sh.
dnf install -y --setopt=strict=0 \
    gcc libxml2-devel libcurl-devel openssl-devel nss-devel \
    rh-python38-python-devel

# rh-python38 ships pip 19.3; refresh it so it can talk to current PyPI
# and resolve the sdists cleanly.
"${PY}" -m pip install --upgrade pip

# pycurl must link the same SSL backend as the system libcurl (NSS on
# el7). Tell its build explicitly so import does not fail with a
# "link-time/compile-time libcurl SSL backend mismatch" error.
export PYCURL_SSL_LIBRARY=nss

"${PY}" -m pip install "ovirt-engine-sdk-python==${SDK_VERSION}"

# Fail loudly here rather than later inside the smoke test if the build or
# import did not work.
"${PY}" -c 'import ovirtsdk4; print("ovirtsdk4 OK:", ovirtsdk4.version.VERSION)'
