# myCareer source notice

Origin: the user-supplied `myCareer-main.zip` snapshot, not a guessed Git revision.
`SOURCE_MANIFEST.json` records SHA-256 hashes of all preserved original files.
The upstream `package.json` declares **ISC**; the supplied root contained no separate
LICENSE file. Its declaration and source comments are preserved without inventing
an author or copyright notice. ApplyPilot's original license is unchanged.

`runtime.py` is an unchanged copy of `share/chatgpt/mycareer.py`; its validation,
normalization, tier and target-selection functions are called by the career adapter.
The original JSON schema and adjacency catalog are used at runtime. The matcher,
projection and interview adapters in `applypilot/career/` port the corresponding
JavaScript and interview/document protocols, with deliberately stricter safeguards:
adjacent control evidence is NOT a certification or equivalence, external verification
is fact-scoped, restricted prose requires explicit review, and model output cannot
add new canonical resume bullets. Original JS and protocol files are retained for
maintenance and cross-implementation comparison, not launched as a second app.

The example corpus is synthetic upstream demonstration data, NEVER installed as
the user's profile or career evidence. It is supplied only for validation tests.
The SQLite/D1 service and platform applications were not included or integrated.
