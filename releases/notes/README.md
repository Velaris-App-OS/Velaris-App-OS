# Release Notes

One file per release: `vX.Y.Z.md`

## Versioning

| Change | Version bump |
|--------|-------------|
| Bug fixes, minor improvements | Patch — `1.0.0` > `1.0.1` |
| New features, non-breaking changes | Minor — `1.0.0` > `1.1.0` |
| Breaking changes, major rewrites | Major — `1.0.0` > `2.0.0` |

## Release checklist

1. Write `releases/notes/vX.Y.Z.md`
2. Add new migrations to `migrations/`
3. Update `releases/manifest.sql` if activating new features
4. Bump version in `velaris.yaml`
5. Update `releases.txt`
6. `git commit` + `git tag vX.Y.Z` + `git push --tags`
7. Create GitHub Release on that tag
