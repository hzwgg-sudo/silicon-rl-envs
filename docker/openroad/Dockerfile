# Pinned OpenROAD runtime profile (M1-01).
#
# Reproducible execution inputs for GCD/nangate45. The base image is pinned
# by digest (never by floating tag) and the ORFS checkout is pinned by
# commit. Rebuilding or re-pulling this profile needs a Linux x86_64 host
# with a container runtime; it is NOT attempted on the Mac dev laptop.

FROM docker.io/openroad/orfs:26Q2@sha256:7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61

ARG ORFS_TAG=26Q2
ARG ORFS_COMMIT=036d106273e66855cd5214d49518fd0f0df7de61

WORKDIR /OpenROAD-flow-scripts/flow

# Check that the digest-pinned image carries the expected layout. The image
# omits Git metadata; this is not a revision probe. Use the clean pinned
# source checkout and preflight in docs/gcd-quickstart.md for scored runs. The flow itself is launched
# explicitly (see silicon_env/environments/openroad/README.md), never as
# part of the image build.
RUN test -f ./Makefile \
    && test -f ./designs/nangate45/gcd/config.mk \
    && echo "digest-pinned image layout is present; expected ORFS ${ORFS_TAG} (${ORFS_COMMIT})"

CMD ["make", "DESIGN_CONFIG=./designs/nangate45/gcd/config.mk"]
