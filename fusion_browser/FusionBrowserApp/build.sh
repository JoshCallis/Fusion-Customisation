#!/bin/bash
# Compile and install FusionBrowser.app — see bin/build_app.sh (shared by every window shell).
exec "$(dirname "$0")/../../bin/build_app.sh" FusionBrowser "$(dirname "$0")/main.swift" 
