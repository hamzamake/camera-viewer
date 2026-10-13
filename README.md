# Camera Recorder

A lightweight desktop app that records multiple RTSP camera streams in rolling segments, with audio.

## Requirements

- **Python 3.8+** (stdlib only — no pip installs needed)
- **ffmpeg** installed and on your PATH

### Install ffmpeg

| OS | Command |
|----|---------|
| Ubuntu / Debian | `sudo apt install ffmpeg` |
| macOS (Homebrew) | `brew install ffmpeg` |
| Windows | Download from https://ffmpeg.org/download.html and add to PATH |

## Run

```bash
python recorder.py
```

## Usage

### Dashboard tab
- Shows a live card for each enabled camera
- Status updates as each segment is written
- Current output file path shown per camera

### Settings tab
- **Output Directory** — where video folders are created (one subfolder per camera)
- **Segment Length** — how long each video file is (1 / 3 / 5 / 10 minutes)
- **Cameras** — add, remove, rename, or disable cameras; edit RTSP URLs
- Hit **Save Settings** — changes persist across restarts (`~/.camera_recorder_config.json`)

## Output structure

```
~/CameraRecordings/
├── Camera_1/
│   ├── Camera_1_2025-01-15_09-00-00.mp4
│   ├── Camera_1_2025-01-15_09-03-00.mp4
│   └── ...
└── Camera_2/
    ├── Camera_2_2025-01-15_09-00-00.mp4
    └── ...
```

Each file contains video **and audio** (copied directly from the stream, no re-encoding).

## Notes

- If a camera goes offline, the recorder retries automatically after 5 seconds
- Stopping recording finalises any in-progress segment
- Config is saved at `~/.camera_recorder_config.json`
