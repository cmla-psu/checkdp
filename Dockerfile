FROM debian:stable AS builder

COPY . /checkdp
WORKDIR /checkdp

# build KLEE and PSI
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
    sudo \
    gcc \
    g++ \
    make \
    atfs \
    libtinfo-dev \
    libtinfo5 \
    libxml2-dev \
    cmake \
    curl \
    git \
    unzip \
    zlib1g-dev \
    libsqlite3-dev \
    xz-utils \
    ca-certificates \
    wget

RUN bash scripts/build_klee.sh
RUN bash scripts/build_psi.sh
RUN bash scripts/get_z3.sh

# use clean image to install checkdp
FROM debian:stable-slim

RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-setuptools \
    gcc \
    libtinfo5 \
    libxml2 \
    libc-dev \
    libz3-dev \
    libsqlite3-dev \
    libgoogle-perftools-dev \
    curl && \
    # and tini
    TINI_VERSION=`curl https://github.com/krallin/tini/releases/latest | grep -o "/v.*\"" | sed 's:^..\(.*\).$:\1:'` && \
    curl -L "https://github.com/krallin/tini/releases/download/v${TINI_VERSION}/tini_${TINI_VERSION}.deb" > tini.deb && \
    dpkg -i tini.deb && \
    rm tini.deb && \
    apt-get remove -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*


COPY . /checkdp
# copy compiled KLEE and PSI into this image
COPY --from=builder /checkdp/klee /checkdp/klee
COPY --from=builder /checkdp/psi /checkdp/psi

WORKDIR /checkdp

RUN pip3 install --no-cache-dir .

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["/bin/bash"]
