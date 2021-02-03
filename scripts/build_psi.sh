#!/bin/bash
set -e

git clone https://github.com/eth-sri/psi.git
cd psi
./dependencies-release.sh && ./build-release.sh