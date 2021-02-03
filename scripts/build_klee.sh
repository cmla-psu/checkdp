#!/bin/bash
set -e

# environment setup and install dependencies
if [[ "$OSTYPE" == "linux-gnu" ]]; then
  LLVM_PLATFORM="linux-gnu-ubuntu-18.04"
  LIB_EXT="so"
  CORES=$(nproc --all)
  BUILD_KLEE="make -j${CORES}"
  sudo apt-get -y install gcc g++ bison flex cmake wget libz3-dev libgoogle-perftools-dev libsqlite3-dev xz-utils zlib1g-dev atfs libtinfo-dev libtinfo5 libxml2-dev
elif [[ "$OSTYPE" == "darwin"* ]]; then
  LLVM_PLATFORM="apple-darwin"
  LIB_EXT="dylib"
  CORES=$(sysctl -n hw.ncpu)
  # TODO: remove if there are better ways to fix missing /usr/include in macOS mojave/catalina
  BUILD_KLEE="C_INCLUDE_PATH=$(xcrun --show-sdk-path)/usr/include make -j${CORES}"
  brew install cmake wget z3 gperftools
else
  echo "Platform not supported: ${OSTYPE}"
  exit 1
fi

set -e
echo "Cloning KLEE"
git clone https://github.com/klee/klee.git
cd klee
git checkout tags/v2.2  # fix KLEE version to v2.2
mkdir deps && cd deps
echo "Downloading LLVM-9.0"
LLVM_DIR="llvm-9.0"
wget -q https://releases.llvm.org/9.0.0/clang+llvm-9.0.0-x86_64-${LLVM_PLATFORM}.tar.xz
tar xf clang+llvm-9.0.0-x86_64-${LLVM_PLATFORM}.tar.xz
mv clang+llvm-9.0.0-x86_64-${LLVM_PLATFORM} ${LLVM_DIR}
rm clang+llvm-9.0.0-x86_64-${LLVM_PLATFORM}.tar.xz

echo "Cloning MiniSAT"
git clone https://github.com/stp/minisat.git
cd minisat
echo "Building MiniSAT"
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j"${CORES}"
cd ../../

echo "Cloning STP"
git clone https://github.com/stp/stp.git
cd stp
git checkout tags/2.3.3
mkdir build && cd build
cmake -DMINISAT_LIBRARY=../../minisat/build/libminisat.${LIB_EXT} -DMINISAT_INCLUDE_DIR=../../minisat -DCMAKE_BUILD_TYPE=Release ..
make -j"${CORES}"
cd ../../../

echo "Now building KLEE"
mkdir build && cd build
cmake -DLLVM_CONFIG_BINARY=../deps/${LLVM_DIR}/bin/llvm-config -DKLEE_RUNTIME_BUILD_TYPE=Release -DENABLE_SOLVER_Z3=ON -DENABLE_SOLVER_STP=ON -DSTP_DIR=../deps/stp/build -DENABLE_UNIT_TESTS=OFF -DENABLE_SYSTEM_TESTS=OFF -DCMAKE_BUILD_TYPE=Release ../
eval "$BUILD_KLEE"
