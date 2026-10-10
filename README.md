# Clipnest

Clipnest is a local-only web interface for downloading videos from sites supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp). It uses FFmpeg to combine separate audio/video streams when needed. Support varies by website and can change when a site's implementation changes; this does not mean every social-media link will work.

## Requirements

- Python 3.9 or newer
- FFmpeg installed and available on `PATH`
- Network access to the source site

Install the Python dependency and start the app:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:8000>. The server binds to loopback only and saves finished files in the local `downloads` folder. Download jobs and their files are removed after two hours when the app is next accessed.

## Behavior and limits

- Paste a supported video page URL, choose **Best available** or a maximum resolution (1080p, 720p, 480p, or 360p), and select **Add to queue**. The queue shows each job's position and progress, with a Save link when each download finishes.
- A selected resolution is an upper limit; the source may only provide lower resolutions. yt-dlp selects matching video/audio streams and FFmpeg merges them where needed.
- Downloads are limited to 2 GB each and run one at a time. Add up to 50 jobs to the queue; each job keeps the quality selected when it was added.
- The app ignores local yt-dlp configuration and proxy settings. If a `cookies.txt` file exists beside `app.py`, yt-dlp uses it for the download; otherwise downloads use no sign-in cookies. The cookie file stays on this computer and is not sent to the webpage.
- To provide cookies, export them in yt-dlp's Netscape cookies format to `cookies.txt` beside `app.py`, then restart Clipnest. Cookies grant access to your account, so keep the file private and remove it when you no longer need it. Authentication does not bypass a site's access restrictions.
- Only download media you own or are authorized to save, and follow the source site's terms and applicable law.
- The footer links to a supported-platform PDF generated from the installed yt-dlp extractor list. Refresh it after updating yt-dlp with `python build_supported_platforms.py`.

The app does not promise support for every platform or video. Source sites may block downloads, change their systems, require sign-in, restrict content, or return errors. Clipnest does not upload URLs or files to a Clipnest service, though yt-dlp must contact the URL's host to retrieve the requested media.
