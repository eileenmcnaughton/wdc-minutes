# WDC meetings catalogue

A catalogue of every Whangārei District Council meeting we have a listing for: what eScribe publishes, what is mirrored in this repo, and what each meeting discussed.

## Files

| File | What it holds |
|---|---|
| `meetings.json`, `meetings/YYYY.json` | One record per meeting (all years, or one file per year): `id`, `meeting`, `date`, `time`, `venue`, `topics`, `items` (numbered agenda items with title, page and topic tags) and `documents` (each with `kind`, eScribe `escribe_doc_id`/`escribe_url`, repo `path`, `in_repo`, `url`, and `mentions` counts for flagged terms). |
| `documents.json` | **The full list of expected documents**, one row each: `date`, `meeting`, `category`, `label` (eScribe's name), `filename`/`path` (the repo copy), `repo_url` (link to the mirrored copy, or null), `escribe_url` + `escribe_doc_id` (the original, or null where the listing had no link yet), `url` (the best available link), `in_repo`, `meeting_id`, `time`, `venue`. Repo files not tied to a meeting (annual plans and reports, odd attachments) appear with `category: "Other"`. |
| `topics.json` | Topic → every agenda item tagged with it, newest first. |
| `missing-urls.txt` | eScribe URLs for listed documents that are not in the repo yet. |
| `COVERAGE.md` | Meetings and documents by meeting type and year: listed, mirrored, missing, and missing without a known link. |
| `sources/` | The eScribe listings the catalogue is built from (see below). |
| `build_catalog.py` | Rebuilds everything above. |

`url` points at the GitHub Pages copy when the file is mirrored, otherwise at eScribe.

## Rebuilding

```bash
python3 catalog/build_catalog.py . --write-repo-index    # from the repo root; needs pdftotext (poppler)
```

`--write-repo-index` also regenerates the files the WDC-analyst GPT reads, all kept compact so an Action can load them:

- `index.json`: every mirrored document (`date`, `meeting`, `category`, `filename`, `path`, `url`, plus `meeting_id`, `escribe_id` and `mentions`).
- `minutes.json`: every set of minutes we can link to, using the mirrored copy first and eScribe as the fallback.
- `data.json`: the source list, i.e. the two files above, `catalog/topics.json` and one `catalog/meetings/YYYY.json` per year.

The GitHub workflow (`.github/workflows/build-index.yml`) runs this on every push that adds PDFs or listings, and daily. Text extracted from PDFs is cached in `catalog/.textcache/` (git-ignored and kept in the Actions cache), so rebuilds after the first are quick.

## Adding listings

Drop new eScribe listings into `catalog/sources/` and rebuild. Three formats are read:

- **`*.txt`**: a listing copied from the eScribe portal *with* links, as Markdown, e.g. `* [PDF](https://pub-wdc.escribemeetings.com/FileStream.ashx?DocumentId=5458)`.
- **`*.nolinks.txt`**: a listing copied as plain text, with lines like `AgendaPDF` / `MinutesPDF` / `Agenda Cover PagePDF` and attachment names. These give the meeting, time, venue and document list, but no download links. A later listing with links fills the links in.
- **`*.psv`**: `meeting|date|time|venue|kind|doc_id|label`, with kind one of Agenda, Addendum, Minutes, Cover or Attachment.

The eScribe entries from the original hand-built `index.json` and `minutes.json` are kept in `sources/legacy-index.psv`, so nothing recorded there is lost now that both files are generated.

## Agenda items and topics

Agenda items come from the table of contents on the first pages of each mirrored agenda (or the minutes, if there is no agenda), plus any addendum. Each item is tagged against the topic list in `TOPICS` at the top of `build_catalog.py`. These are plain regular expressions, so edit them to add or split topics. `FLAGS` defines terms counted across the full text of every mirrored PDF (currently T2 / transit lane), which catches documents that discuss a topic without it being an agenda item, such as the operations reports.

Meetings with no mirrored agenda or minutes have no items yet. They fill in once the PDFs are added.

## Known limits

- eScribe truncates attachment names (`Item 4.1 Attachment 1 W....pdf`). Attachments are linked to repo files by the longest matching name prefix; generic ones such as `Presentation....pdf` stay unlinked.
- Two briefings on the same day are told apart by time, but repo file names carry no time, so both map to the first.
