# Releasing Memento Vault

## Release smoke gate

Before cutting or reviewing release-related changes, run the fast safe smoke gate:

```bash
python3 scripts/release_smoke.py
```

The default gate checks CLI help/version paths, MCP module help startup, Homebrew formula metadata, pi package metadata, and version consistency across `VERSION`, `package.json`, `memento/__init__.py`, and `Formula/memento-vault.rb`. It only runs non-mutating checks and prints actionable failure messages.

Optional tool-backed checks are separated behind `--heavy`:

```bash
python3 scripts/release_smoke.py --heavy
```

Heavy checks currently validate Docker Compose config and run an npm package dry-run when those tools are installed; missing optional tools are reported as skips.

## Version tag

1. Run `python3 scripts/release_smoke.py` and fix any failures.
2. Make sure `VERSION`, `package.json`, `memento/__init__.py`, and `Formula/memento-vault.rb` agree on the release version.
3. Merge the release PR to `main`.
4. Tag the release from `main`:

```bash
git checkout main
git pull --ff-only origin main
git tag vX.Y.Z
git push origin vX.Y.Z
```

## GitHub release

Draft release notes from the commits since the previous tag:

```bash
git log --oneline vPREVIOUS..vX.Y.Z
```

Create the GitHub release with the drafted notes.

## Homebrew tap

The public tap is `sandsower/homebrew-tap`.

1. Download the release tarball and compute its checksum:

```bash
curl -L -o memento-vault-vX.Y.Z.tar.gz \
  https://github.com/sandsower/memento-vault/archive/refs/tags/vX.Y.Z.tar.gz
shasum -a 256 memento-vault-vX.Y.Z.tar.gz
```

2. Copy `Formula/memento-vault.rb` to the tap repo.
3. Update the formula:

```ruby
url "https://github.com/sandsower/memento-vault/archive/refs/tags/vX.Y.Z.tar.gz"
sha256 "<computed sha256>"
```

4. Test the formula locally where Homebrew is available:

```bash
brew install --build-from-source ./Formula/memento-vault.rb
brew test memento-vault
memento-vault version
```

5. Commit and push the tap update.

Users can then install with:

```bash
brew tap sandsower/tap
brew install memento-vault
memento-vault install
```
