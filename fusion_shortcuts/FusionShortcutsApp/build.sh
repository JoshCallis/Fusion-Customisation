#!/bin/bash
# Compile and install FusionShortcuts.app — see bin/build_app.sh (shared by every window shell).
exec "$(dirname "$0")/../../bin/build_app.sh" FusionShortcuts "$(dirname "$0")/main.swift" 
