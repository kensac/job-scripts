# Job Scripts

This repository contains a set of Python scripts for automating the tracking and management of job postings. The tools here fetch job data from a JSON source, apply configurable filters, and record new opportunities in a Google Sheets document.

## Contents

* **`pittcsc_simplify.py`**
  Main script that:

  * Authenticates with Google Sheets via service account
  * Fetches job postings from a JSON endpoint
  * Applies filters for location, terms, duplicates, and activity
  * Appends new, qualifying postings to a specified Google Sheet

* **Filter Summary**
  Within `pittcsc_simplify.py` you will find `summarize_filters()`, which can log or report detailed filter results, including:

  * Locations excluded
  * Terms excluded
  * Counts of postings omitted by each rule
  * Active postings that passed all filters

## Prerequisites

* **Python** 3.9 or higher
* **Google Sheets API** enabled on a service account with a JSON key file
* Access to the job listings JSON endpoint

## Installation

1. Clone this repository:

   ```bash
   git clone https://github.com/kensac/job-scripts.git
   cd <repository-directory>
   ```
2. Create and activate a virtual environment __(optional but recommended)__:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

## Configuration

Create a `.env` file in the project root with the following entries:

```ini
SHEET_ID=<your-google-sheet-id>
JOB_LISTINGS_URL=<json-api-endpoint-url>
GOOGLE_APPLICATION_CREDENTIALS_CUSTOM=<path-to-service-account-key.json>
```

* **`SHEET_ID`**: The ID of the Google Sheet where new postings will be appended.
* **`JOB_LISTINGS_URL`**: URL of the JSON feed providing job postings.
* **`GOOGLE_APPLICATION_CREDENTIALS_CUSTOM`**: Path to your service account key file.

**Note**: Ensure the service account has write access to the target Google Sheet. This can be done by sharing the sheet with the service account email. The email can be found in the service account JSON file under the `client_email` field.

## Usage

To fetch, filter, and append new job postings, run:

```bash
python pittcsc_simplify.py
```

By default, the script will:

1. Authenticate and open the target sheet.
2. Fetch postings and parse them into structured data.
3. Filter out inactive, location-excluded, term-excluded, old, and duplicate entries.
4. Append all qualifying postings to the sheet.
5. Log a summary of each filter operation.

## Customization

* **Filters**

  * Modify `EXCLUDED_LOCATIONS` and `INCLUDED_TERMS` in `pittcsc_simplify.py` to adjust which postings are kept or dropped.
* **Fallback Date**

  * Change `FALLBACK_CUTOFF_DATE` to control how entries with missing term metadata are handled based on posting date.
* **Logging Level**

  * Adjust the `logging.basicConfig` level to control verbosity (e.g., `INFO`, `DEBUG`, `ERROR`).


## Troubleshooting
If you encounter issues:
1. Ensure your Google Sheets API is correctly set up and the service account has access to the target sheet.
2. Check the JSON endpoint URL for correctness and availability.
3. Verify that the `.env` file is correctly configured with the necessary credentials and IDs.
4. Review the logs for any error messages or warnings that can help diagnose the problem.


## Notes and comments
* At the time of writing, the script is designed to work with the specific JSON structure provided by the simplified Pitt CSC job postings repository. If the structure changes, you may need to adjust the parsing logic accordingly.
* I also use this repo to track job postings for myself using github actions. If you want to use this script for your own job tracking optionally, you can set up a GitHub action to run the script on a schedule (e.g., daily or weekly) to keep your job postings up-to-date. This requires addional setup to configure GitHub secrets for the `.env` variables.

## Contributing
Contributions are welcome! Please open an issue or submit a pull request if you have suggestions or improvements.

## Issues
If you encounter any issues, please check the [issues page](https://github.com/kensac/job-scripts/issues) and feel free to open a new issue if your problem is not listed.

## Acknowledgments
Thanks to the Simplify and Pitt CSC teams for providing the job postings data and the inspiration for this script.


## License

This project is licensed under the MIT License. See `LICENSE` for details.
