# English–German quality sample

`golden_en.txt` and `golden_de_ref.txt` contain 20 aligned sentence pairs from
**FLORES-200**, `devtest`, languages `eng_Latn` and `deu_Latn`. Each output line
corresponds to the same one-based line in the two original corpus files, in this
order:

`25, 75, 125, 175, 225, 275, 325, 375, 425, 475, 525, 575, 625, 675, 725, 775, 825, 875, 925, 975`.

This is a fixed, evenly spaced subset (line `25 + 50n`, `n = 0..19`) chosen to
keep a two-model live measurement inexpensive. The downloaded release contains
1,012 aligned `devtest` lines per language. The archive's README says language
files and `metadata_devtest.tsv` share sentence order. The German prose is
copied verbatim from that release; it was not authored or translated for this
project.

## Source and integrity

- Publisher: NLLB Team / Meta AI, via the [facebookresearch/flores FLORES-200
  release](https://github.com/facebookresearch/flores/blob/main/flores200/README.md).
- Official release archive: <https://dl.fbaipublicfiles.com/nllb/flores200_dataset.tar.gz>
  (S3 version ID `KJZoZnGvyduN3osrx.67C_UQ7C9oNDic`, last modified
  2022-07-14 as served on retrieval).
- Retrieved: **2026-10-03**.
- Archive SHA-256: `b8b0b76783024b85797e5cc75064eb83fc5288b41e9654dabc7be6ae944011f6`.
- Archive member `./flores200_dataset/devtest/eng_Latn.devtest` SHA-256:
  `612e9fbe87997617c0fa8fa8929654a4f49b728d96738112c2b86ef6a1d78d88`.
- Archive member `./flores200_dataset/devtest/deu_Latn.devtest` SHA-256:
  `9a1bfa90d153fedb7ff80047f4a63d493998888eddb056e8a28e24592f26e5f2`.
- Committed `golden_en.txt` SHA-256:
  `de86f8502d684186e59af057fccf63c3809ec266b262047b720ed7bad6b2d362`.
- Committed `golden_de_ref.txt` SHA-256:
  `dd161c3776e1fd239ec8eb9e0161088dd9bb8b6a1b65527e37e24c75cef09795`.

To reproduce, download the archive, verify its SHA-256, read the two named
members as UTF-8, select the listed one-based lines, append the exact suffixes
below to **both** language files at the matching source line, then join the 20
lines with `\n` and one final newline. No normalization or other text editing
was applied.

## Adaptation and interpretation

The following **synthetic literal-only suffixes** were appended, byte for byte,
to the matching English and German corpus sentences. They are for the
placeholder/date/currency/number preservation check; they are not natural
language reference translations:

| Original `devtest` line | Identical suffix (including leading space) |
| --- | --- |
| 25 | ` {{customer_id}} %s` |
| 75 | ` 2026-10-03 03/10/2026` |
| 125 | ` $19.95 €7.50 USD 42.00 EUR 8.00 12% 5% 7 7` |

The suffixes exercise two placeholder forms, two date formats, dollar and euro
symbols, USD and EUR codes, percentages, and a repeated plain number. They
provide **synthetic token coverage only**; they do not establish performance on
real document fields, locale-formatted amounts, tables, long files, PDF text
reflow, or fallback pages. Since the suffixes are identical in both languages,
they may slightly increase chrF relative to untouched FLORES prose. This
20-sentence sample is a narrow quality comparison, not a representative corpus
score or a document-fidelity measurement.

## Attribution and license

FLORES-200 is published by the NLLB Team / Meta AI in Meta Platforms, Inc.'s
`facebookresearch/flores` repository. Cite: **NLLB Team et al. (2022), “No
Language Left Behind: Scaling Human-Centered Machine Translation”**, as
requested in the [publisher's README](https://github.com/facebookresearch/flores#citation).
The release metadata identifies original web articles; copyright in those
articles may belong to their respective contributors. This attribution does
not imply endorsement by Meta, the NLLB Team, or the article contributors.

The publisher explicitly lists **FLORES-200 under Creative Commons Attribution–
ShareAlike 4.0 International (CC BY-SA 4.0)** in its [license
table](https://github.com/facebookresearch/flores#licenses). That license
permits redistribution and adaptation with attribution, a license link, a
change notice, and ShareAlike terms. The corpus-derived contents of the two
sample text files, including the disclosed adaptations, are redistributed
under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Downstream
adaptations of those contents must use a compatible ShareAlike license. The
surrounding application source code has its own repository terms.
