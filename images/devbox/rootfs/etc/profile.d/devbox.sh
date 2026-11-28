# Login shells (bb reads the login-shell PATH to find provider CLIs): keep
# $HOME's installs ahead of the image's, matching the container's ENV PATH.
for d in /usr/local/go/bin "$HOME/go/bin" "$HOME/.cargo/bin" "$HOME/.bun/bin" "$HOME/.local/bin" "$HOME/.local/share/mise/shims"; do
  case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH" ;; esac
done
export PATH
export NPM_CONFIG_PREFIX="$HOME/.local" BUN_INSTALL="$HOME/.bun" GOPATH="$HOME/go" MISE_YES=1
