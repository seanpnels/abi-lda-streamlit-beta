# ABI OOH LDA Compliance Hub — Stage 5

Stage 5 is a **separate Streamlit beta** using a private GitHub release as the shared OSM dataset. **Do not overwrite the existing v9 Streamlit app.** It uses the same candidate screening principles as Stage 4 but adds 250/500-ft selection, 50-ft review band, a map with distance ruler, Excel and My Maps CSV exports, and data-freshness reporting.

## Step A — publish the dataset (POC repo)

In `seanpnels/abi-lda-data-poc`, add **only**:
- `scripts/check_dataset.py`
- `.github/workflows/ny-osm-stage5-data.yml`

Keep your existing Stage 1–4 scripts and workflows. Do not upload real inventory or the `inputs/` folder again. The Stage 5 workflow is scheduled daily at 10:00 UTC, and can also be started manually from Actions. It downloads Geofabrik NY State, filters OSM candidates, validates the data, and creates a **private GitHub Release** with the compressed dataset and metadata. The workflow must have **Settings → Actions → General → Workflow permissions → Read and write permissions** to publish releases. This is repo-wide and should be approved by your repository owner. If it fails at publishing, inspect the error before changing permissions.

Run **NYC OSM daily screening dataset - Stage 5** manually once. In the repository's **Releases** page, confirm a `nyc-osm-...` release with `nyc-facilities.geojsonseq.gz` and `manifest.json`. GitHub releases are durable but not a production database; future iterations should handle retention and access controls.

## Step B — deploy the Streamlit beta (separate repo)

Create a **separate private GitHub repository**, e.g. `abi-lda-stage5-app`. Upload from this ZIP:
- `streamlit_app.py`
- `requirements.txt`
- `.streamlit/config.toml` (GitHub browser Create new file path if hidden folder upload fails)
- `.gitignore` (optional)

Deploy **this repo** as a separate app in Streamlit Community Cloud; main file path: `streamlit_app.py`.

In Streamlit **App settings → Secrets**, set:

```toml
GITHUB_REPO = "seanpnels/abi-lda-data-poc"
GITHUB_TOKEN = "YOUR_FINE_GRAINED_GITHUB_TOKEN"
```

Create a **fine-grained GitHub personal access token** limited to the private POC repo, with **Contents: Read-only**. Never paste the token into chat or commit it to GitHub. The app uses the token server-side to read the newest published private dataset release. If GitHub requires token approval, obtain it from the repository administrator.

**Security:** Streamlit Community Cloud apps are accessible by URL unless access is explicitly restricted. Do **not** upload confidential inventory to this beta without your organization's approval and suitable authentication. Streamlit Secrets hide the token from app viewers but **do not restrict who can use the app**. The app does not write inventory to GitHub, but uploads still pass through the hosted Streamlit service.

## Known limitations

- NYC-only coordinate envelope, not statewide screening; placements outside NYC envelope are **gray**.
- Candidate polygons are **not verified building footprints**; points/lines remain uncertain.
- School, worship, daycare, university, and limited substance-use candidate tags; incomplete OSM is unavoidable.
- 250/500-ft and 50-ft review logic, with **human review mandatory**.
- Green means **no detected conflict**, never cleared/approved.
- A 48-hour stale snapshot **blocks screening** until a successful refresh.
- The same snapshot is reused across users, but hosting resource limits still apply.
- Ruler measures map distance visually, not a legal survey.
- Stage 5 does not yet show full restriction polygons or facility-specific map pins; those are follow-on UI work.
- Source freshness is tied to the OSM extract timestamp, not just GitHub workflow completion.

## Troubleshooting

- `No Stage 5 dataset release found`: run the Stage 5 data workflow and confirm it published a release.
- `403`: token lacks Contents read permission, or workflow lacks Contents write permission to publish.
- `Dataset failed validation`: stop and inspect workflow; do not treat as clean coverage.
- If the app cannot read a private GitHub release asset, inspect the exact error; do not expose the token.
