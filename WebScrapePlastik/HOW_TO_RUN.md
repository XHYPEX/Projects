# How to Run This App

## One-time setup (do this only once)

### 1. Install Docker Desktop
- Go to https://www.docker.com/products/docker-desktop/
- Download and install it (it's free)
- Open Docker Desktop and wait until it says **"Docker is running"** in the bottom left

### 2. Open a Terminal in this folder

**On Mac:**
- Open the folder in Finder
- Right-click on an empty area → "New Terminal at Folder"
  *(or open Terminal and drag the folder into it)*

**On Windows:**
- Open the folder in File Explorer
- Click the address bar at the top, type `cmd`, press Enter

### 3. Run this one command

```
docker compose up --build
```

- The first time this runs it will take **5–10 minutes** to download everything
- You will see a lot of text scrolling — that is normal
- When you see `You can now view your Streamlit app in your browser`, it is ready

### 4. Open the app

Open your browser and go to:
```
http://localhost:8501
```

---

## Every time after that

Just run:
```
docker compose up
```
(No `--build` needed after the first time — it will start much faster)

---

## To stop the app

Press `Ctrl + C` in the terminal window.
