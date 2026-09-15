# Skill-only installation and companion tools

`npx skills add` installs the selected skill directory. Do not assume it also installs the repository-level scripts or exports `SKILLS_BUNDLE_ROOT`. The consumer instructions in `SKILL.md`, `references/`, and `evals/` work as guidance; helper commands and TAP fixtures need the separate public companion checkout below. No sibling skill installation is required.

For ordinary CLI inspection, use the installed CLI and its `--help` directly. Read the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup) directly when checking versions; the website checker is optional automation. Only fetch the companion when a routed workflow actually needs source preparation, the GraphQL helper, structural validators, or fixtures.

## Prepare the public companion

Requires Git, Python 3, anonymous HTTPS access, and a writable temporary directory. Run this block in the same Bash session as the subsequent helper commands. It fetches a fixed revision of the public Skills repository, not private implementation sources. Stop if any command fails; do not fall back to a guessed host checkout.

```bash
SKILLS_BUNDLE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/nexus-skills-companion.XXXXXXXX")" &&
git -C "$SKILLS_BUNDLE_ROOT" init -q &&
git -C "$SKILLS_BUNDLE_ROOT" remote add origin https://github.com/Talus-Network/skills &&
git -C "$SKILLS_BUNDLE_ROOT" fetch --depth 1 origin f28f0b03c771572430672c18ea27c92d27dbba98 &&
git -C "$SKILLS_BUNDLE_ROOT" checkout --detach -q FETCH_HEAD &&
test "$(git -C "$SKILLS_BUNDLE_ROOT" rev-parse HEAD)" = f28f0b03c771572430672c18ea27c92d27dbba98 &&
test -f "$SKILLS_BUNDLE_ROOT/scripts/prepare_sources.py" &&
test -f "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" &&
test -f "$SKILLS_BUNDLE_ROOT/scripts/docs_website.py" &&
export SKILLS_BUNDLE_ROOT
```

This revision supplies the companion executables; continue following the installed skill's current guidance for CLI use and evidence limitations. Resolve every repository-level `scripts/...` command below `$SKILLS_BUNDLE_ROOT`. TAP-specific `scripts/` and `fixtures/` paths resolve below `$SKILLS_BUNDLE_ROOT/nexus-tap-development`, even when those files are absent from the installed skill directory. Run unqualified companion commands from that checkout, or use the explicit absolute path.

Keep the companion checkout for subsequent gates. The source-preparation reference's manifest cleanup removes only the downloaded source workspace, not this companion checkout. Record the temporary checkout path; remove it when no remaining gate needs it. If fetching is unavailable, report the helper/fixture gate as blocked and continue applicable direct CLI or Docs reads.

## Source and chain boundaries

The companion repository supplies tooling only. Source preparation still downloads only the reviewed public SDK, Move Packages, and Sui archives using the source manifest and cleanup procedure. Use the explicit official Testnet endpoint for the read-only GraphQL helper. Neither fetching the companion nor running a validator authorizes a network transaction.

