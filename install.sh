#!/usr/bin/env bash
# Ярлык в меню приложений + команда `helm` в ~/.local/bin
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons
ln -sf "$DIR/helm.sh" ~/.local/bin/helm
cp "$DIR/helm.svg" ~/.local/share/icons/helm.svg
cat > ~/.local/share/applications/helm.desktop <<D
[Desktop Entry]
Name=HELM
GenericName=SSH Commander
Comment=SSH, SFTP, Docker и systemd в одном окне
Exec=$DIR/helm.sh
Icon=$HOME/.local/share/icons/helm.svg
Terminal=false
Type=Application
Categories=Network;System;Development;
Keywords=ssh;sftp;docker;server;terminal;
D
echo "Готово: запускайте 'helm' или через меню приложений."
