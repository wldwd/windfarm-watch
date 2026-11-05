# Wind Farm Watch

A one-page website about wind farm approvals near Moodiarrup, plus a checker that reads the official websites twice a day (6:17 am and 5:17 pm Perth time). When it finds something new about Ambrosia, West Arthur Wind Farm or Bellwether, it:

1. adds it to the top of the website, and
2. opens a GitHub issue, which GitHub emails to you.

The first check only records what is already there. Alerts start from the second check.

## Setup (about 10 minutes)

**1. Make the repository**
- On github.com, click **New repository**.
- Give it a name that doesn't identify the family (for example `windfarm-watch`).
- Choose **Public**. Free GitHub Pages needs a public repository.
- Leave "Add a README" unticked, then click **Create repository**.

**2. Upload the files**

Either use the website:
- Unzip the folder. On a Mac, press **Cmd + Shift + .** in Finder to show the hidden `.github` folder.
- On the new repository page, click **uploading an existing file**.
- Drag in everything inside the folder, including `.github`, and click **Commit changes**.
- Check the repository now shows a `.github/workflows/watch.yml` file. If it doesn't, use **Add file → Create new file**, type `.github/workflows/watch.yml` as the name, and paste the file's contents in.

Or use Terminal, run from inside the unzipped folder:
```
git init -b main
git add .
git commit -m "Wind Farm Watch"
git remote add origin https://github.com/YOUR-USERNAME/windfarm-watch.git
git push -u origin main
```

**3. Turn on the website**
- Go to **Settings → Pages**.
- Under **Build and deployment → Source**, choose **GitHub Actions**.

**4. Let the checker save its results**
- Go to **Settings → Actions → General**.
- Under **Workflow permissions**, choose **Read and write permissions**, then click **Save**.

**5. Get email alerts** (optional, recommended)
- Go to **Settings → Secrets and variables → Actions → Variables → New repository variable**.
- Name it `NOTIFY_USERS`. For the value, enter your GitHub username with an @, for example `@yourname`. Add more usernames separated by spaces if family members have GitHub accounts.
- At the top of the repository, click **Watch → All Activity**.

**6. Run it once**
- Go to the **Actions** tab and choose **Check approvals and publish site**.
- Click **Run workflow**.
- After a minute or two, the run summary shows the website address: `https://YOUR-USERNAME.github.io/windfarm-watch/`. Send that link to the family.

## Everyday use

- **Change the summary or add a comment window.** Open `data/status.json` on GitHub, click the pencil, edit, and commit. The site updates within a couple of minutes.
  - Dates are written as `2026-11-20`.
  - A window with `"opens"` and `"closes"` dates shows as *Coming up*, *Open now* (with days left) or *Closed* automatically.
  - Use `null` for dates that aren't known yet.
- **Add a website to check.** Add an entry to `watch/sources.json`. `match` holds the words that make a line worth reporting.
- **When an alert arrives**, open the source and confirm it. Then add the window to `data/status.json` so the family sees the closing date at the top of the page.
- **Clear an alert** by closing the issue on GitHub.

## Things to know

- **The site is public.** Anyone with the link can see it, and search engines are asked not to list it. Keep names, the address, offers and negotiation details off it.
- **Some websites block automatic reading or need JavaScript.** The EPBC portal probably will. The section at the bottom of the site, "Which websites the checker could read", shows any it couldn't read. Check those by hand now and then.
- **The checker works by matching words**, so it can miss things or raise false alarms. It doesn't replace a call to the Shire (08 9736 2400) or a letter from the developer.
- **GitHub sometimes runs scheduled checks late** by up to an hour. If a repository goes quiet for 60 days, GitHub may pause the schedule and email you. Re-enable it from the Actions tab with one click.
- **To test the checker on your computer**, run `python3 watch/watch.py` (needs `pip install pypdf`).
