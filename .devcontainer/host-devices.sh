#!/bin/sh
#
# Resolve this host's radios to two colon-free names, on the host, before the
# container is created (devcontainer.json's initializeCommand).
#
# Why this exists: a by-id name can contain colons — this host's ESP32-S3
# carries a MAC address in its name — and devcontainer.json's
# `${localEnv:NAME:default}` is split at the first colon of the *default*, which
# silently truncates the path to something that does not exist. So the names go
# here, in a shell script, where a colon is just a character, and what
# devcontainer.json mounts are the two symlinks this leaves behind:
#
#     $HOME/.cache/sighop-devcontainer/modem-0  ->  the ESP32-S3 board
#     $HOME/.cache/sighop-devcontainer/modem-1  ->  the USB-serial board
#
# A host with different radios exports SIGHOP_DEVCONTAINER_MODEM_0 and
# SIGHOP_DEVCONTAINER_MODEM_1. A host with none exports nothing and gets
# /dev/null, so the container still starts — only the commands that need a
# modem are unavailable. See .devcontainer/README.md.

set -u

# This host's two radios, by their stable by-identifier names.
DEFAULT_MODEM_0=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_90:70:69:85:AD:28-if00
DEFAULT_MODEM_1=/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0

dir="${HOME}/.cache/sighop-devcontainer"
mkdir -p "$dir" || exit 1

link_device() {
    name=$1
    wanted=$2
    fallback=$3

    if [ -n "$wanted" ]; then
        if [ ! -e "$wanted" ]; then
            echo "sighop dev container: ${name}: ${wanted} does not exist on this host" >&2
            exit 1
        fi
        target=$wanted
    elif [ -e "$fallback" ]; then
        target=$fallback
    else
        # Not an error: a checkout is developed on hosts with no radio attached.
        echo "sighop dev container: ${name}: no radio attached, using /dev/null" >&2
        target=/dev/null
    fi

    ln -sfn "$target" "${dir}/${name}"
}

link_device modem-0 "${SIGHOP_DEVCONTAINER_MODEM_0:-}" "$DEFAULT_MODEM_0"
link_device modem-1 "${SIGHOP_DEVCONTAINER_MODEM_1:-}" "$DEFAULT_MODEM_1"
