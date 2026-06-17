# OceanWave3D sandbox image — builds the solver from the three user-supplied
# third-party source tarballs and runs it inside the container.
#
# This mirrors the native build performed by src/oceanwave_mcp/installer.py
# (same legacy gfortran flags, same `make Release` arguments), so a simulation
# run in the container is identical to one run from a native build. It exists so
# a user only needs Docker + the three tarballs — no Fortran toolchain, no MSYS2.
#
# Build context (assembled automatically by docker_runner.build_image; can also
# be staged by hand — see docker/README in the repo README) must contain:
#   Dockerfile
#   Harwell.tar.gz          (Harwell Subroutine Library / HSL — licensed)
#   SPARSKIT2.tar.gz        (SPARSKIT2, Y. Saad)
#   lapack-3.3.1.tgz        (LAPACK 3.3.1 reference + BLAS)
#   OceanWave3D-Fortran90/  (the Fortran source submodule, sans build/ & VCS dirs)
#
# Manual build: drop the three tarballs at the repo root (they are gitignored
# there) and build with the repo root as the context:
#   docker build -t oceanwave3d-mcp:latest -f docker/Dockerfile .
# The COPY paths below are context-root-relative; .dockerignore trims the rest.
# (The automated MCP build stages an equivalent clean context for you.)
#
# Run a simulation (the MCP does this for you):
#   docker run --rm --mount type=bind,source=<run_dir>,target=/work \
#       oceanwave3d-mcp:latest input.inp
# writes fort.* / LOG.txt / Kinematics*.bin into <run_dir> (bind-mounted /work).

# ---------------------------------------------------------------------------
# Stage 1 — builder: compile the libraries and link the solver.
# ---------------------------------------------------------------------------
# Ubuntu 22.04 ships gfortran 11. The legacy flag -fallow-argument-mismatch
# (needed to compile this 1990s–2011 code on a modern compiler) requires
# gfortran >= 10, so the original image's 20.04 base (gfortran 9) is bumped here.
FROM ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Only the Fortran toolchain is needed — the source and tarballs are COPYed in,
# so no wget/git/network access during the build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gfortran make ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Legacy flags so 1990s–2011 Fortran compiles cleanly on gfortran 11 — identical
# to installer.LEGACY_FFLAGS / OW3D_OPTFLAGS. OPTFLAGS adds -fno-automatic
# (the solver relies on static locals).
ENV LEGACY="-std=legacy -fallow-argument-mismatch -ffree-line-length-none"
ENV OW3D_OPTFLAGS="-O2 -std=legacy -fallow-argument-mismatch -ffree-line-length-none -fno-automatic"

WORKDIR /build

# --- [1/4] LAPACK + BLAS -> /usr/local/lib/liblapack.a, libblas.a -----------
# --strip-components=1 makes us independent of the archive's top-level dir name.
COPY lapack-3.3.1.tgz /build/
RUN mkdir -p lapack \
 && tar -xzf lapack-3.3.1.tgz -C lapack --strip-components=1 \
 && cd lapack \
 && cp make.inc.example make.inc \
 && sed -i "s|^OPTS *=.*|OPTS = -O2 ${LEGACY}|"  make.inc \
 && sed -i "s|^NOOPT *=.*|NOOPT = -O0 ${LEGACY}|" make.inc \
 && make blaslib \
 && make lapacklib \
 && cp "$(find . -name 'blas*.a'   | head -1)" /usr/local/lib/libblas.a \
 && cp "$(find . -name 'lapack*.a' | head -1)" /usr/local/lib/liblapack.a

# --- [2/4] SPARSKIT2 -> /usr/local/lib/libskit.a ----------------------------
COPY SPARSKIT2.tar.gz /build/
RUN mkdir -p sparskit \
 && tar -xzf SPARSKIT2.tar.gz -C sparskit --strip-components=1 \
 && cd sparskit \
 && make F77=gfortran OPT="-c -O2 ${LEGACY}" \
 && cp libskit.a /usr/local/lib/

# --- [3/4] Harwell -> /usr/local/lib/libharwell.a ---------------------------
# Drop any shipped .o (may be from an old compiler); the makefile's default
# target archives straight into installd.
COPY Harwell.tar.gz /build/
RUN mkdir -p harwell \
 && tar -xzf Harwell.tar.gz -C harwell --strip-components=1 \
 && cd harwell \
 && rm -f *.o \
 && make FF=gfortran FFLAGS="-O3 ${LEGACY} -fno-automatic" installd=/usr/local/lib \
 && test -f /usr/local/lib/libharwell.a

# --- [4/4] OceanWave3D -> /usr/local/bin/OceanWave3D ------------------------
# Command-line make assignments override common.mk's per-user blocks exactly as
# installer.build_oceanwave3d does. SHELL=/bin/bash because the makefile recipes
# use bash-only `&>` redirections that dash (Ubuntu's /bin/sh) mishandles.
COPY OceanWave3D-Fortran90 /build/OceanWave3D-Fortran90
WORKDIR /build/OceanWave3D-Fortran90
RUN mkdir -p build \
 && make Release \
      SHELL=/bin/bash \
      FC=gfortran \
      BUILDDIR=/build/OceanWave3D-Fortran90/build \
      INSTALLDIR=/usr/local/bin \
      PROGNAME=OceanWave3D \
      LIBDIRS="-L/usr/local/lib" \
      LINLIB="-lharwell -lskit -llapack -lblas" \
      OPTFLAGS="${OW3D_OPTFLAGS}" \
 && test -x /usr/local/bin/OceanWave3D

# ---------------------------------------------------------------------------
# Stage 2 — runtime: just the binary plus the gfortran runtime libraries.
# ---------------------------------------------------------------------------
FROM ubuntu:22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# libgfortran5 (with libquadmath0 / libgcc-s1) is all the dynamically-linked
# binary needs at run time; the static libs (harwell/skit/lapack/blas) are
# baked into the executable.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgfortran5 libquadmath0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/OceanWave3D /usr/local/bin/OceanWave3D

# Each run bind-mounts its isolated run directory here; the solver reads the
# input file (argv[1]) and writes fort.* / LOG.txt / Kinematics*.bin into cwd.
WORKDIR /work
ENTRYPOINT ["OceanWave3D"]
CMD ["input.inp"]
