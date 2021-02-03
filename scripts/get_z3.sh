#!/bin/bash
set -e

Z3_VERSION="4.8.10"

if [[ "$OSTYPE" == "linux-gnu" ]]; then
  Z3_PLATFORM="ubuntu-18.04"
elif [[ "$OSTYPE" == "darwin"* ]]; then
  Z3_PLATFORM="osx-10.15.7"
else
  echo "Platform not supported: ${OSTYPE}"
  exit 1
fi

wget https://github.com/Z3Prover/z3/releases/download/z3-${Z3_VERSION}/z3-${Z3_VERSION}-x64-${Z3_PLATFORM}.zip
unzip z3-${Z3_VERSION}-x64-${Z3_PLATFORM}.zip
rm z3-${Z3_VERSION}-x64-${Z3_PLATFORM}.zip
mv z3-${Z3_VERSION}-x64-${Z3_PLATFORM} z3