# AI Job Hunter: Quick Setup Guide

Follow these steps to configure the AI Job Hunter with your own profile and receive automated hourly job updates.

## 1. Clone the repository

Open a terminal and run:

```bash
git clone https://github.com/Snigdha-Gayathri/ai-job-hunter.git
cd ai-job-hunter
```

If you want to use GitHub Actions for automatic hourly execution, first **fork the repository** into your own GitHub account and clone your fork instead.

## 2. Add your personal details

Navigate to the `config/` directory.

Copy:

`profile.example.json`

Rename the copy to:

`profile.private.json`

Open `profile.private.json` and fill in your own information:

- Name and professional headline
- Education and experience
- Technical and other skills
- Projects and achievements
- Target job roles
- Preferred locations and remote-work preferences
- Companies you want to exclude
- Local resume path
- Optional video resume URL

**Important:** Always edit `profile.private.json`, never the shared example file. Your private profile is excluded from Git and should never be committed or pushed.

## 3. Install dependencies

Make sure Python 3.10 or later is installed.

```bash
python -m venv venv
```

Activate the environment:

Windows:
```bash
venv\Scripts\activate
```

macOS/Linux:
```bash
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## 4. Configure your API credentials

Set up your own credentials. You will need:

- Groq API key
- Apify API token
- Gmail address
- Gmail App Password

Configure these as environment variables locally, or use a local `.env` file if supported by the existing setup.

Never use someone else's API keys or Gmail credentials.

## 5. Run the job hunter locally

Once your profile and credentials are configured:

```bash
python agent/main.py --once
```

This runs one job-search cycle.

For a dry run:

```bash
python agent/main.py --dry-run
```

## 6. Enable automatic hourly execution using GitHub Actions

To receive automated hourly job updates:

1. Fork the repository into your own GitHub account.
2. Open your fork on GitHub.
3. Navigate to **Settings → Secrets and variables → Actions**.
4. Select **New repository secret**.
5. Add the following secrets using your own credentials:

| Secret | Value |
|---|---|
| `PROFILE_JSON` | Your complete `profile.private.json` contents |
| `GROQ_API_KEY` | Your Groq API key |
| `APIFY_API_TOKEN` | Your Apify API token |
| `GMAIL_USERNAME` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Your Gmail App Password |

6. Open the **Actions** tab and enable GitHub Actions if prompted.
7. Select the **Hourly Job Hunter** workflow.
8. Click **Run workflow** to trigger your first run.

After successful setup, the workflow automatically runs every hour according to its configured schedule.

You can monitor execution and errors under the **Actions** tab.

## Important Notes

- Every user must configure their own profile and API credentials.
- GitHub forks do not inherit the original repository's Actions secrets.
- Your private profile and resume must never be committed.
- The hourly workflow runs independently in your own GitHub repository.
- Automated email delivery requires valid Gmail credentials and the appropriate application settings.
- Check the workflow's configured schedule for its timezone. GitHub Actions cron schedules use UTC.
