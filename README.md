# Sri Lanka Digital Industry News

A news website for the Digital Industry Development team. It updates once a day. Every
morning it collects Sri Lankan news about startups, SMEs, tech and IT/BPM, the digital
economy, digitalization and the economy. Gemini screens the stories for relevance,
assigns each one a category and writes a short summary. The day's stories appear on a
web page that anyone with the link can open, with no login.

- **Updates:** daily at about 7:00–7:30 AM Sri Lanka time, ready by 8:00 AM
- **Shows:** stories from the last 24 hours, with title, outlet, author, date and time,
  category and summary. Clicking a story opens the original article.
- **Filters:** by category, plus search and "most relevant first"
- **Archive:** earlier days stay available from the *Edition* menu
- **Cost:** free (GitHub Actions, GitHub Pages and the Gemini API free tier)

```
config/sources.yaml     ← which outlets and searches to follow   (edit freely)
config/settings.yaml    ← categories, AI settings, relevance threshold (edit freely)
pipeline/               ← the Python code that runs every morning
site/index.html         ← the website (one page)
site/data/              ← today's stories (latest.json) + archive (written by the pipeline)
state/state.json        ← remembers what was already shown, so stories don't repeat
.github/workflows/      ← the daily schedule
tests/                  ← automated tests (python -m pytest)
```

---

## Setup (about 15 minutes, once)

### 1. Create the repository
1. On GitHub, create a new repository, for example `sl-digital-news`. Make it **public**.
   GitHub Pages is free for public repositories. Private repos need a paid plan.
2. Upload these files:
   ```bash
   cd sl-digital-news
   git init && git add . && git commit -m "Initial version"
   git branch -M main
   git remote add origin https://github.com/<your-username>/sl-digital-news.git
   git push -u origin main
   ```

### 2. Get a Gemini API key
1. Go to **https://aistudio.google.com**, sign in, and choose **Get API key → Create API key**.
2. In your GitHub repo, go to **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `GEMINI_API_KEY`
   - Value: your key

### 3. Turn on the website and the schedule
1. **Settings → Pages → Build and deployment → Source:** choose **GitHub Actions**.
2. **Settings → Actions → General → Workflow permissions:** choose **Read and write permissions**, then Save.

### 4. Run it the first time
1. Open the **Actions** tab. Choose **Daily news update** → **Run workflow** → **Run workflow**.
2. Wait 10–20 minutes. When it finishes, the site is at
   **`https://<your-username>.github.io/sl-digital-news/`**. Share this link with the team.
3. Scroll to the bottom of the site and open **Source status for this edition** to see which
   sources worked (see "Maintaining it" below).

After that it runs every morning by itself.

### 5. (Optional) Let the team submit social media posts
LinkedIn, Instagram and Facebook pages such as Examiner or Catalist can't be read
automatically. Team members can submit those posts through a Google Form instead. Submitted
posts are summarized and appear in the next morning's edition.

1. Create a **Google Form** with these questions:
   - **Link to the post** (short answer, required)
   - **Title** (short answer, optional)
   - **Source / page name** (short answer, e.g. "Examiner")
   - **Paste the post text** (paragraph, optional, but recommended. The AI can't open
     LinkedIn or Instagram, so it summarizes from this text.)
2. In the form, go to **Responses → Link to Sheets**.
3. In that Google Sheet, go to **File → Share → Publish to web**. Choose the responses sheet
   and **Comma-separated values (.csv)**, then **Publish**. Copy the URL.
4. In GitHub, go to **Settings → Secrets and variables → Actions → Variables tab → New repository variable**:
   - `SUBMISSIONS_CSV_URL` = the published CSV URL
   - `SUBMIT_FORM_URL` = the form's share link. This adds a **"+ Submit a link"** button to the site.

---

## Maintaining it

### Add, remove or fix a news source
Edit `config/sources.yaml` and commit. You don't need to change any code.
- **Outlet with a feed:** add an entry with `type: rss` and its feed URL. Add a
  `fallback_query: "site:domain.lk"` so that if the feed breaks, Google News covers that
  outlet instead.
- **Outlet without a feed:** add an entry with `type: google_news` and a `site:` query.
- **New topic to watch** (a new accelerator, programme or company): add a line under `topics:`.
- **A source keeps failing:** set `enabled: false` on it.

The site footer's **Source status** table shows, after each run, whether each source
worked and how many items it returned. Some feed URLs in the starter list couldn't be tested
before launch. Check this table after the first run.

### Tune what counts as relevant
In `config/settings.yaml`:
- `min_relevance` is the lowest score (0–10) that still appears on the site. Default `3`,
  which keeps anything remotely relevant. Raise it to `5` for a shorter list.
- `categories` sets the filter buttons on the site. Each `description` tells the AI what
  belongs in that category.

### Gemini free tier
- The default model is `gemini-3.5-flash-lite`. To switch models without editing code, add a
  repository **variable** `GEMINI_MODEL`.
- A normal day uses roughly 15–40 Gemini requests, because stories are sent in batches. Your
  project's exact limits are listed in Google AI Studio under **Rate limits**.
- If the daily quota runs out partway through a run, the run doesn't fail. Keyword rules
  categorize the remaining stories and their summaries come from the outlet's own snippet.
  The footer then shows `daily free-tier quota reached`. To use fewer requests, increase
  `triage_batch_size` and `summary_batch_size` in `settings.yaml`.
- Note: on the free tier, Google may use what you send to improve its products. Here that is
  only public news text.

### Run it manually
- **Actions → Daily news update → Run workflow.** You can enter a number of hours, for example
  `48` after an outage, to look back further.
- Locally:
  ```bash
  pip install -r requirements.txt
  export GEMINI_API_KEY=...            # optional; without it, keyword rules are used
  python -m pipeline.run --check-sources   # just test every source
  python -m pipeline.run                   # full run (writes site/data/)
  cd site && python -m http.server 8000    # preview at http://localhost:8000
  ```
- Tests: `pip install pytest && python -m pytest -q`

### Good to know
- **Anyone with the link can view the site.** The page asks search engines not to index it.
  To limit it to your team later, put it behind Cloudflare Access (free for small teams).
  Team members would then sign in with a one-time email code.
- **Author names** come from the outlet's feed or article page. Many Sri Lankan outlets don't
  credit an author online, so those cards show only the outlet.
- **Exact times:** if an outlet doesn't publish a time, the card shows just the date.
- Summaries are AI-generated and kept short, and every card links to the original article.
- GitHub pauses scheduled workflows in repos with no activity for 60 days. The daily data
  commit counts as activity, so this shouldn't happen. If it ever does, re-enable the
  workflow in the Actions tab.

## How it works

```
7:00 AM (GitHub Actions)
  1. Collect     outlet RSS feeds + Google News searches (Sri Lanka edition) + form submissions
  2. Filter      only stories published since the last run; skip anything already shown;
                 a keyword pre-filter for general-news outlets (drops sport, crime, etc.)
  3. Dedupe      same link or near-identical headline → one card, "Also reported by: …"
  4. Screen      Gemini scores relevance 0–10 in batches of 40 headlines
  5. Read        for relevant stories: real article link, author, time, article text
  6. Summarise   Gemini picks a category and writes a 2–3 sentence summary (batches of 10)
  7. Publish     site/data/latest.json + archive → committed → GitHub Pages
```
