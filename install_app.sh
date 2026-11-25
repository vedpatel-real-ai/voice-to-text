#!/bin/bash
echo "install_app.sh is obsolete. VoiceToText now has one supported path: ./install.sh"
exec "$(cd "$(dirname "$0")" && pwd)/install.sh" "$@"
