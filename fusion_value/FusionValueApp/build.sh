#!/bin/bash
# Compile and install FusionValue.app — see bin/build_app.sh (shared by every window shell).
exec "$(dirname "$0")/../../bin/build_app.sh" FusionValue "$(dirname "$0")/main.swift" --agent
