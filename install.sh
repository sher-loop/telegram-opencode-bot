#!/data/data/com.termux/files/usr/bin/bash
#
# install.sh — install OpenCode natively on Termux (aarch64, no proot)
#
# Bridges the glibc-requiring official OpenCode binary with Termux's
# bionic libc by:
#   1. installing glibc + build/runtime deps via pkg
#   2. downloading the official opencode-linux-arm64 release
#   3. compiling a tiny C bootstrapper that runs the real binary
#      through the glibc dynamic loader
#
# Usage:  bash install.sh     (from a fresh F-Droid Termux, aarch64)
#
set -euo pipefail

# ── Termux paths ──────────────────────────────────────────────────
readonly PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
readonly HOME_DIR="${HOME:-/data/data/com.termux/files/home}"
readonly TMP_DIR="${TMPDIR:-${PREFIX}/tmp}"

readonly OT_DATA_DIR="${XDG_DATA_HOME:-${HOME_DIR}/.local/share}/opencode-termux"
readonly OT_BIN_DIR="${OT_DATA_DIR}/bin"
readonly OT_CACHE_DIR="${XDG_CACHE_HOME:-${HOME_DIR}/.cache}/opencode-termux"
readonly OPENCODE_REAL_BIN="${OT_BIN_DIR}/opencode"
readonly OPENCODE_BOOTSTRAPPER="${PREFIX}/bin/opencode"

readonly GLIBC_LOADER="${PREFIX}/glibc/lib/ld-linux-aarch64.so.1"
readonly GLIBC_LIB_PATH="${PREFIX}/glibc/lib"
readonly GLIBC_LIBC="${GLIBC_LIB_PATH}/libc.so.6"
readonly GLIBC_REPO_FILE="${PREFIX}/etc/apt/sources.list.d/glibc.list"
readonly SSL_CERT_FILE="${PREFIX}/etc/tls/cert.pem"

readonly OPENCODE_API="https://api.github.com/repos/anomalyco/opencode/releases/latest"
readonly TARBALL_NAME="opencode-linux-arm64.tar.gz"

# ── Colors ────────────────────────────────────────────────────────
CLR_RESET=$'\033[0m'
CLR_BOLD=$'\033[1m'
CLR_GREEN=$'\033[32m'
CLR_YELLOW=$'\033[33m'
CLR_RED=$'\033[31m'
CLR_DIM=$'\033[2m'

log_ok()   { printf '%s[*] %s%s\n' "$CLR_GREEN" "$1" "$CLR_RESET"; }
log_info() { printf '%s[.] %s%s\n' "$CLR_BOLD" "$1" "$CLR_RESET"; }
log_warn() { printf '%s[w] %s%s\n' "$CLR_YELLOW" "$1" "$CLR_RESET"; }
log_err()  { printf '%s[x] %s%s\n' "$CLR_RED" "$1" "$CLR_RESET"; }

require_termux() {
    [[ -d "${PREFIX}/bin" && -n "${PREFIX}" ]] || {
        log_err "This script must be run inside Termux."
        exit 1
    }
}

check_architecture() {
    local arch
    arch="$(uname -m)"
    if [[ "$arch" != "aarch64" ]]; then
        log_err "Unsupported architecture: ${arch}"
        log_err "Official OpenCode only ships aarch64 builds. Refusing to continue."
        exit 1
    fi
    log_ok "Architecture: aarch64"
}

check_existing() {
    if [[ -x "$OPENCODE_BOOTSTRAPPER" ]]; then
        log_warn "OpenCode is already installed (${OPENCODE_BOOTSTRAPPER})"
        [[ -f "${OT_DATA_DIR}/.version" ]] && log_info "Version installed: $(cat "${OT_DATA_DIR}/.version")"
        printf '%s\n' "  Reinstall? [y/N] "
        read -r answer
        if [[ ! "$answer" =~ ^[sSyY]$ ]]; then
            log_info "Installation cancelled."
            exit 0
        fi
    fi
}

install_glibc() {
    if [[ ! -f "$GLIBC_REPO_FILE" ]] || [[ ! -d "${PREFIX}/etc/apt/sources.list.d" ]]; then
        log_info "Installing glibc-repo..."
        pkg install glibc-repo -y
    fi
    if [[ ! -f "$GLIBC_LIBC" ]]; then
        log_info "Installing glibc..."
        pkg install glibc -y
    fi
    log_ok "glibc ready"
}

install_deps() {
    local deps=(git ripgrep python clang jq nodejs-lts curl tar)
    local pkg missing=0
    log_info "Verifying dependencies..."
    for pkg in "${deps[@]}"; do
        case "$pkg" in
            ripgrep)   bin="rg" ;;
            nodejs-lts) bin="node" ;;
            python)    bin="python" ;;
            *)         bin="$pkg" ;;
        esac
        if command -v "$bin" &>/dev/null; then
            log_ok "$pkg (already installed)"
        else
            log_info "Installing $pkg..."
            if ! pkg install "$pkg" -y; then
                log_err "Failed to install $pkg"
                missing=$((missing + 1))
            else
                log_ok "$pkg installed"
            fi
        fi
    done
    if [[ $missing -gt 0 ]]; then
        log_err "${missing} dependency(ies) could not be installed."
        exit 1
    fi
    log_ok "All dependencies ready"
}

get_latest_version() {
    curl -fsSL "$OPENCODE_API" | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/'
}

download_opencode() {
    local version url tmp_tarball
    version="$(get_latest_version)" || {
        log_err "Could not fetch the latest OpenCode version from GitHub."
        exit 1
    }
    log_info "Latest OpenCode version: ${version}"

    mkdir -p "$OT_CACHE_DIR" "$OT_BIN_DIR"
    url="https://github.com/anomalyco/opencode/releases/download/${version}/${TARBALL_NAME}"
    tmp_tarball="${OT_CACHE_DIR}/${TARBALL_NAME}"

    log_info "Downloading OpenCode ${version}..."
    if ! curl -fsSL --progress-bar "$url" -o "$tmp_tarball"; then
        log_err "Download failed:"
        log_err "  $url"
        exit 1
    fi

    log_info "Extracting binary..."
    tar -zxf "$tmp_tarball" -C "$OT_BIN_DIR"
    rm -f "$tmp_tarball"

    if [[ ! -f "$OPENCODE_REAL_BIN" ]]; then
        log_err "OpenCode binary not found after extraction."
        ls -la "$OT_BIN_DIR"
        exit 1
    fi
    chmod +x "$OPENCODE_REAL_BIN"
    echo "$version" >"${OT_DATA_DIR}/.version"
    log_ok "OpenCode ${version} ready in ${OT_BIN_DIR}"
}

compile_bootstrapper() {
    local src="${OT_CACHE_DIR}/opencode_helper.c"
    local q
    q() { printf '"%s"' "$1"; }

    mkdir -p "$OT_CACHE_DIR"
    log_info "Writing bootstrapper source..."
    cat >"$src" <<'HELPER_EOF'
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <limits.h>

#ifndef GLIBC_LOADER
#define GLIBC_LOADER ""
#endif
#ifndef OPENCODE_REAL_BIN
#define OPENCODE_REAL_BIN ""
#endif
#ifndef GLIBC_LIB_PATH
#define GLIBC_LIB_PATH ""
#endif
#ifndef SSL_CERT_PATH
#define SSL_CERT_PATH ""
#endif

int main(int argc, char** argv) {
    unsetenv("LD_PRELOAD");
    unsetenv("LD_LIBRARY_PATH");

    setenv("GODEBUG", "netdns=cgo", 1);
    setenv("SSL_CERT_FILE", SSL_CERT_PATH, 1);

    char exec_path[PATH_MAX];
    ssize_t len = readlink("/proc/self/exe", exec_path, sizeof(exec_path) - 1);
    if (len == -1) return 1;
    exec_path[len] = '\0';

    char* loader = GLIBC_LOADER;
    char* real_bin = OPENCODE_REAL_BIN;
    char* lib_path = GLIBC_LIB_PATH;

    char** new_argv = malloc((size_t)(argc + 4) * sizeof(char*));
    if (!new_argv) return 1;

    new_argv[0] = loader;
    new_argv[1] = "--library-path";
    new_argv[2] = lib_path;
    new_argv[3] = real_bin;

    for (int i = 1; i < argc; i++)
        new_argv[i + 3] = argv[i];
    new_argv[argc + 3] = NULL;

    execv(loader, new_argv);
    perror("execv");
    free(new_argv);
    return 1;
}
HELPER_EOF

    log_info "Compiling bootstrapper..."
    clang -O2 -o "$OPENCODE_BOOTSTRAPPER" "$src" \
        -DGLIBC_LOADER="$(q "$GLIBC_LOADER")" \
        -DOPENCODE_REAL_BIN="$(q "$OPENCODE_REAL_BIN")" \
        -DGLIBC_LIB_PATH="$(q "$GLIBC_LIB_PATH")" \
        -DSSL_CERT_PATH="$(q "$SSL_CERT_FILE")"
    chmod +x "$OPENCODE_BOOTSTRAPPER"

    if [[ ! -x "$OPENCODE_BOOTSTRAPPER" ]]; then
        log_err "Bootstrapper is not executable."
        exit 1
    fi
    log_ok "Bootstrapper installed at ${OPENCODE_BOOTSTRAPPER}"
}

verify_install() {
    log_info "Verifying installation..."
    if command -v opencode &>/dev/null; then
        log_ok "opencode is on your PATH at $(command -v opencode)"
    else
        log_warn "opencode not found in PATH."
    fi

    printf '%s\n' ""
    log_ok "OpenCode installed successfully!"
    log_ok "Run:  opencode --help"
    printf '%s\n' ""
}

main() {
    require_termux
    check_architecture
    check_existing
    install_glibc
    install_deps
    download_opencode
    compile_bootstrapper
    verify_install
}

main "$@"