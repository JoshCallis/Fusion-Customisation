#!/bin/bash
# Compile and install FusionDock.app — see bin/build_app.sh (shared by every window shell).
exec "$(dirname "$0")/../../bin/build_app.sh" FusionDock "$(dirname "$0")/main.swift" --agent
