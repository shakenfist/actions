#!/bin/bash
# Pin postgresql-jdbc to a version oVirt 4.4's bundled Spring can use.
#
# oVirt 4.4 (engine 4.4.10.7) bundles spring-jdbc 5.0.4. When the engine
# backend boots it resolves every stored function's signature through
# Spring's SimpleJdbcCall, whose GenericCallMetaDataProvider calls
# java.sql.DatabaseMetaData.getProcedures() and HARD-THROWS
# "Unable to determine the correct call signature - no
# procedure/function/signature for '<fn>'" when that returns no rows.
#
# On PostgreSQL 11+, real PROCEDUREs exist, so pgjdbc reports PL/pgSQL
# FUNCTIONS (oVirt's stored procedures are all functions) under
# getFunctions(), not getProcedures(). pgjdbc dropped functions from
# getProcedures() between 42.2.10 (still returns them) and 42.2.12 (does
# not). Current el8 AppStream ships 42.2.14, so on a fresh el8 host the
# 4.4 engine dies at the very first function call (gettagsbyparent_id,
# from TagsDirector.init) and ovirt-engine.service never reaches DB-up.
#
# oVirt 4.5 is unaffected because it bundles spring-jdbc 5.3.x, which
# also consults getFunctions(); only the frozen 4.4 Spring is brittle.
# We cannot swap the Spring inside the engine ear, so we pin the driver.
#
# el8 shipped postgresql-jdbc-42.2.3 when oVirt 4.4 was current, and
# 42.2.3 still reports functions under getProcedures() -- i.e. it is the
# driver the 4.4 engine was actually validated against. We install that
# exact RPM from the CentOS vault and versionlock it so the later
# ovirt-engine pull / any dnf update cannot drag it back to 42.2.14.
#
# This must run AFTER ovirt-engine is installed (that pull is what brings
# in 42.2.14) and BEFORE engine-setup starts the backend.
#
# Override the RPM with PGJDBC_RPM_URL if the vault path ever moves.
#
# Usage: ovirt-pin-pgjdbc.sh

set -xe
export PS4='=======================\n+ '

PGJDBC_RPM_URL="${PGJDBC_RPM_URL:-http://vault.centos.org/8.5.2111/AppStream/x86_64/os/Packages/postgresql-jdbc-42.2.3-3.el8_2.noarch.rpm}"

# versionlock keeps the downgrade sticky: without it engine-setup (which
# may pull dependencies) or a later dnf update would upgrade the driver
# straight back to the broken 42.2.14.
sudo dnf install -y python3-dnf-plugin-versionlock

# Clear any pre-existing lock on the package so the downgrade is not
# itself blocked, then downgrade to the validated 42.2.3 build.
sudo dnf versionlock delete postgresql-jdbc || true
sudo dnf -y downgrade "${PGJDBC_RPM_URL}"

# Re-lock at the now-installed (42.2.3) version.
sudo dnf versionlock add postgresql-jdbc

# Self-verify: the engine only works with a driver whose getProcedures()
# still reports functions, which is everything up to and including
# 42.2.10. Bail loudly if a 42.2.12+ driver somehow remains.
installed="$(rpm -q --qf '%{VERSION}' postgresql-jdbc)"
case "${installed}" in
    42.2.3|42.2.4|42.2.5|42.2.6|42.2.7|42.2.8|42.2.9|42.2.10)
        echo "postgresql-jdbc pinned to ${installed} (getProcedures reports functions)" ;;
    *)
        echo "ERROR: postgresql-jdbc is ${installed}; oVirt 4.4's spring-jdbc" \
             "5.0.4 needs <= 42.2.10 or the engine backend will not start." >&2
        exit 1 ;;
esac

rpm -q --qf '%{NAME} %{VERSION}-%{RELEASE}\n' postgresql-jdbc
echo "jar: $(rpm -ql postgresql-jdbc | grep -E 'postgresql.*\.jar$' | head -1)"
