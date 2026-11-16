#!/bin/bash
# Compile and install FusionParams.app — see bin/build_app.sh (shared by every window shell).
exec "$(dirname "$0")/../../bin/build_app.sh" FusionParams "$(dirname "$0")/main.swift" 
