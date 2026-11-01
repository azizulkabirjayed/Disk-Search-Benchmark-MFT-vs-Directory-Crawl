<a id="readme-top"></a>

# About The Project

Disk Search Benchmark is a small Windows app that compares two ways of finding every file on your PC and shows which one is faster. The first way, **Directory Crawl**, opens every folder one by one, the normal way most programs do it. The second way, **MFT Parse**, skips the folders completely and reads the drive's hidden master list of files (the NTFS *Master File Table*) straight from the disk. Press **Run**, and the app scans all your drives both ways and puts the results side by side.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

# Built With

* ![Python](https://img.shields.io/badge/Python-3-3776AB?style=for-the-badge&logo=python&logoColor=white)
* ![Tkinter](https://img.shields.io/badge/Tkinter-GUI-FFD43B?style=for-the-badge&logo=python&logoColor=black)
* ![ctypes](https://img.shields.io/badge/ctypes-Win32%20API-0078D6?style=for-the-badge&logo=windows&logoColor=white)
* ![NTFS](https://img.shields.io/badge/NTFS-Raw%20Volume%20Read-555555?style=for-the-badge&logo=windows&logoColor=white)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

# Project Overview

### Key Features
 
- Scans **every fixed drive** on your PC (internal and external HDDs/SSDs) with one click
- Runs both methods on each drive and compares them in a clean side-by-side table
- Shows time taken, items found, files, folders, items per second, and how many times faster one method is
- One combined progress bar that changes color for each phase (green for MFT, blue for crawl)
- Live numbers while scanning, plus the folder currently being crawled
- A **Stop** button to cancel at any time and keep the partial results
- Dark theme with a dark Windows title bar
- One single Python file, standard library only — nothing to `pip install`

### What The Table Shows

| Row | Meaning |
|---|---|
| Time | Total time taken |
| Items found | Files and folders combined |
| Files / Folders | Each count on its own |
| Items / second | Scan speed |
| Relative speed | How many times faster MFT Parse is |
| Drives scanned | Which drives were checked |

### Project Files

- `project_code.py`: the whole app, in one file

<p align="right">(<a href="#readme-top">back to top</a>)</p>





# How To Run

**Step 1. Install Python**

download it from [python.org](https://www.python.org/downloads/). During setup, tick **"Add python.exe to PATH"** and leave **"tcl/tk and IDLE"** checked, because the app's window needs it.

**Step 2. Run the app**

Open Command Prompt as Administrator, then run:
```bash
cd project_folder_path
python project_code.py
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>



# Sample Visualizations
![Visualization 1](./Images/01.png)
![Visualization 2](./Images/02.png)
![Visualization 3](./Images/03.png)
![Visualization 4](./Images/04.png)
<p align="right">(<a href="#readme-top">back to top</a>)</p>
