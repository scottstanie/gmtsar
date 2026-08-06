FROM ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && apt-get install -y \
    autoconf \
    csh \
    g++ \
    gfortran \
    ghostscript \
    gmt \
    gmt-dcw \
    gmt-gshhg \
    libgmt-dev \
    libhdf5-dev \
    liblapack-dev \
    libtiff5-dev \
    make \
    wget \
    && rm -rf /var/lib/apt/lists/*

COPY . /usr/local/GMTSAR

WORKDIR /usr/local/GMTSAR

RUN autoconf \
    && ./configure --with-orbits-dir=/usr/local/orbits CFLAGS='-z muldefs' LDFLAGS='-z muldefs' \
    && make \
    && make install

# -----------------------------------------------------------
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    csh \
    ghostscript \
    gmt \
    gmt-dcw \
    gmt-gshhg \
    libgmt6 \
    libhdf5-103-1 \
    libhdf5-cpp-103-1 \
    liblapack3 \
    libtiff5 \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy the built GMTSAR installation
COPY --from=builder /usr/local/GMTSAR /usr/local/GMTSAR

ENV GMTSAR=/usr/local/GMTSAR
ENV PATH="${GMTSAR}/bin:${PATH}"

# Create a directory for orbit files (mount or download at runtime)
RUN mkdir -p /usr/local/orbits

WORKDIR /data

CMD ["/bin/bash"]
