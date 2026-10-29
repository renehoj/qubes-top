#!/bin/bash
# Install or update qubes-top on dom0, end to end.
#
# Same route as qubes-systemlist's packaging/install-on-dom0.sh: dom0 has no
# networking, so a staging qube (over `qvm-run -p`) clones the repo fresh
# from the git server -- public read, no credentials -- stamps the checkout
# with the commit it came from, and streams one tarball back. dom0 validates
# the archive, runs the gi audit against it, and only then installs:
#
#   ~/.local/share/qubes-top                        the tree (run via packaging/qubes-top-run.sh)
#   ~/.local/bin/qubes-top                          a two-line wrapper exec'ing the launcher
#   ~/.local/share/applications/qubes-top.desktop   the menu entry, @QUBES_TOP_ROOT@ resolved
#
# The program was called qvm-top until D-26. An install under that name is
# removed here (its tree, its wrapper, its menu entry), and its saved divider
# position moves to ~/.config/qubes-top/.
#
# There is no systemd unit, dbus activation or autostart to install: the app
# is a plain foreground window (DESIGN.md D-8). Running this again is an
# update. A fresh extract is swapped in whole, never overlaid (DESIGN.md M3:
# an overlay leaves deleted files behind).
#
# Consequences worth knowing:
#   * the install tracks origin/main -- committed but not PUSHED work does
#     not deploy;
#   * the staging qube needs `git` and must resolve the git server;
#   * BUILD-COMMIT in the installed tree says exactly which commit it is;
#   * a running qubes-top window keeps the old code until it is reopened.
#
# Trust note: whatever the staging qube hands over becomes code running in
# dom0. If that ever stops feeling like enough, the place to verify a
# signature is step 2, before anything is extracted.
#
# Usage: install-on-dom0.sh [staging-qube]      (default: personal)
#
# Environment overrides:
#   QUBES_TOP_REPO  -- clone URL of qubes-top

set -euo pipefail

QUBES_TOP_URL="${QUBES_TOP_REPO:-https://git.homelab.computer/homelab/qubes-top.git}"
STAGING_VM="${1:-personal}"     # the qube that talks to the git server
TARBALL="qubes-top.tgz"         # local name for the streamed archive
TOP_DIR="qubes-top-build"       # top-level directory inside the tarball
SHARE_DIR="$HOME/.local/share"
BIN_DIR="$HOME/.local/bin"
TREE="$SHARE_DIR/qubes-top"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# 1. Build on the staging qube and stream the tarball back. Everything
#    between the heredoc markers runs THERE, not here. Its stdout is the
#    tarball and nothing else may write to it; set -e makes a failed clone
#    abort before anything installed is touched.
qvm-run -p "$STAGING_VM" "QUBES_TOP_URL='$QUBES_TOP_URL' bash -s" \
    > "$workdir/$TARBALL" <<'EOS'
set -eu
src="$(mktemp -d)"
trap 'rm -rf "$src"' EXIT
mkdir "$src/qubes-top-build"
cd "$src/qubes-top-build"
git clone --quiet --depth 1 "$QUBES_TOP_URL" qubes-top >&2
git -C qubes-top rev-parse HEAD > qubes-top/BUILD-COMMIT
rm -rf qubes-top/.git
tar czf - -C "$src" qubes-top-build
EOS

# 2. Prove the archive is whole, carries its provenance, and that every gi
#    symbol the UI calls resolves on THIS dom0 -- all BEFORE anything
#    installed is touched. The audit runs from the extract, by module.
tar tf "$workdir/$TARBALL" >/dev/null
tar xf "$workdir/$TARBALL" -C "$workdir"
build="$workdir/$TOP_DIR/qubes-top"
for check in qubes_top/__init__.py \
             qubes_top/__main__.py \
             packaging/qubes-top-run.sh \
             packaging/qubes-top.desktop; do
    if [ ! -f "$build/$check" ]; then
        echo "install: $check missing from the archive" >&2
        exit 1
    fi
done
if ! grep -Eq '^[0-9a-f]{40}$' "$build/BUILD-COMMIT"; then
    echo "install: qubes-top carries no valid BUILD-COMMIT" >&2
    exit 1
fi
(cd "$workdir" && PYTHONPATH="$build" \
    python3 -m qubes_top --api-audit)

# 3. Swap. The tree is copied next to its target and renamed into place, so
#    a menu click mid-install never starts a half-written tree.
mkdir -p "$SHARE_DIR"
rm -rf "$SHARE_DIR/.qubes-top.new"
cp -r "$build" "$SHARE_DIR/.qubes-top.new"
rm -rf "$TREE"
mv "$SHARE_DIR/.qubes-top.new" "$TREE"
chmod 755 "$TREE/packaging/qubes-top-run.sh"

# 4. PATH wrapper and menu entry. The wrapper exec's the launcher by its
#    full path rather than symlinking it: qubes-top-run.sh resolves the tree
#    from its own location, which a symlink would move to ~/.local.
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/qubes-top" <<EOF
#!/bin/sh
exec "$TREE/packaging/qubes-top-run.sh" "\$@"
EOF
chmod 755 "$BIN_DIR/qubes-top"
mkdir -p "$SHARE_DIR/applications"
sed "s|@QUBES_TOP_ROOT@|$TREE|" "$TREE/packaging/qubes-top.desktop" \
    > "$SHARE_DIR/applications/qubes-top.desktop"

# 5. Retire an install under the old name (D-26). The wrapper is removed
#    only when it is the one this script wrote, so a qvm-top someone else put
#    on PATH is left alone. The config moves only when the new one does not
#    exist yet, so a second run never overwrites a newer divider position.
OLD_TREE="$SHARE_DIR/qvm-top"
if [ -f "$BIN_DIR/qvm-top" ] && grep -q "$OLD_TREE/packaging/" "$BIN_DIR/qvm-top"; then
    rm -f "$BIN_DIR/qvm-top"
fi
rm -rf "$OLD_TREE" "$SHARE_DIR/.qvm-top.new"
rm -f "$SHARE_DIR/applications/qvm-top.desktop"
if [ -d "$HOME/.config/qvm-top" ] && [ ! -e "$HOME/.config/qubes-top" ]; then
    mv "$HOME/.config/qvm-top" "$HOME/.config/qubes-top"
fi
update-desktop-database "$SHARE_DIR/applications" 2>/dev/null || true

# 6. Prove the installed copy imports through the same wrapper the menu and
#    PATH use (the audit again, now from the installed tree).
"$BIN_DIR/qubes-top" --api-audit >/dev/null

echo "qubes-top $(cut -c1-10 "$TREE/BUILD-COMMIT") installed" \
     "(run: qubes-top, or the qubes-top menu entry)"
