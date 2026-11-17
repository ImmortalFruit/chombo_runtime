# Chombo license scope

Chombo is distributed under multiple licenses. A file-specific license notice,
when present, takes precedence over this document.

## Apache License 2.0

Unless listed below, source code, executable Skill definitions, configuration,
schemas, general documentation, tests, and build tooling are licensed under the
Apache License, Version 2.0. The complete terms are in [`LICENSE`](LICENSE).

## Creative Commons Attribution 4.0 International

The following teaching, domain-knowledge, localization, and media works are
licensed under the Creative Commons Attribution 4.0 International license:

- `concepts/*.json`, excluding `concepts/schemas/`;
- `docs/learning/`, excluding `docs/learning/templates/`;
- `learning/assessments/`, `learning/curriculum/`, and `learning/lessons/`;
- `locales/`;
- `docs/assets/chombo-logo.png` in the development repository and its
  `assets/chombo-logo.png` Runtime Bundle copy;
- `docs/assets/chombo-icon.png`, its `assets/chombo-icon.png` Runtime copy,
  and byte-identical hashed Nuxt copies;
- `ui/app/assets/chombo-dialogue-icon-monochrome.png` and its byte-identical
  hashed Nuxt copies;
- `skills/render-hand/assets/mahjong-tiles/`;
- `assets/point-sticks/`; and
- `assets/sounds/`.

Byte-identical hashed Nuxt build copies of the above media retain the same
license and attribution; they are mapped to their originals by SHA-256.

The complete terms are in
[`LICENSES/CC-BY-4.0.txt`](LICENSES/CC-BY-4.0.txt). Attribute these works to
“Chombo project contributors”, link to CC BY 4.0, and indicate whether changes
were made. Media-specific provenance and hashes are recorded in the asset-rights ledger:
`distribution/ASSET_RIGHTS.md` in the development checkout, or `ASSET_RIGHTS.md`
beside this file in the Runtime Bundle.

The Chombo name and logo may be covered by trademark or other designation
rights. Those rights are not granted by CC BY 4.0, and use of the logo must not
imply project endorsement, affiliation, or official status.

## Third-party material

Bundled third-party Web JavaScript and CSS retain their package-specific licenses.
The generated Runtime inventory and complete package-provided license/notice
texts are at `ui/.output/public/_nuxt/third-party-notices.json`. This inventory
covers rendered client modules and generated build-runtime helpers; it does not
relicense those dependencies under Chombo's default license.

`assets/generated/fonts/YujiBoku/` contains Yuji Boku font software under the
SIL Open Font License 1.1. Its copyright notice and complete license accompany
the font in `assets/generated/fonts/YujiBoku/OFL.txt`. This generation-only
font is not included in the Codex Runtime Bundle.


## Runtime knowledge package

`knowledge/chombo.ckp` contains the existing Concept works and explicitly selected
teaching documents and indexes, plus their internal verification metadata. The
original logical paths and existing licenses still apply: Concept and teaching
works remain CC BY 4.0, attributed to Chombo project contributors; verification
metadata remains Apache 2.0. See LICENSE, LICENSES/CC-BY-4.0.txt and NOTICE.
Compression and reversible obfuscation increase casual extraction effort; they
do not guarantee secrecy or prevent extraction or lawful reuse. No additional
use, modification, redistribution or commercial-use restriction is imposed.
Use the selected Python with `-B -m engine.knowledge_cli --list`, then
`-B -m engine.knowledge_cli --concept <concept_id>` or `--lesson <lesson_id>`.
Reading is in memory and requires no Chombo key or network connection.
