# XP5 Cross-Platform Test Automation & Release Tester
this is my Python/Flask-based test automation tool. 
It is designed to run defined test lists and steps against any application or emulator that provides a window to capture. 


## Overview
Originally built to preserve and run demos across various emulators , with focus put into VICE, Qemu, Hatari, and 86Box in dedicated repositories. this tool has expanded into a full release tester. 

It allows you to test both the emulator(s) and the application itself side by side. This enables the rapid deployment and debugging of software across multiple emulators by assisting in recording, and replaying repeatable build, deploy, and run steps—all from a single web dashboard. 

For example, if you are developing a utility, game, or application, you can test it simultaneously across interesting combinations such as : System 7.5 Mac, MS-DOS, and PlayStation 

it can be run as 1 node in docker, several nodes distributed. or start it standalone on linux/windows/etc

## Key Features
* xp5 testrunner tracks the steps run, the build used, the results found, and generates html reports.
* ubuntu docker container provides XRDP remote desktop access, rdp audio for sound, and OpenSSH - for remote control & access
* Multi-System Testing: Run identical user-driven behavior checks across disparate retro and modern systems at the same time.
* Flexible : Run as a single node in Docker, scale to several distributed nodes, or run standalone.
* Broad OS Support: Tested and working on macOS, Linux, FreeBSD, Windows, OmniOS, and OpenIndiana.
  > Note: NFS on Windows 10 has known limitations (slow performance, lack of inotify).

## Usage Guide

1. Deploy the Container  
   Deploy the XRDP Docker container. The test runner mounts two directories:
   * One for the web application.
   * One for the `testsrc` mount (your project data).  
   *(Note: Directories must reside on a shared filesystem visible to all hosts if using multiple nodes, such as NFS).*

2. Access the Dashboard  
   Connect to your web server:  
   `http://{your-webserver}:8080/`

3. Add Your Tests  
   Place your test scripts in the project directory:  
   `/testsrc/sourcedir/{project-name}/__testlist__86box_{testlist_name}.py`

4. Run and Manage  
   Reload tests directly from the runner homepage, access test directories by name under `/test/`, or use the `v1` API.




<table>
  <tr>
    <td width="35%" valign="middle">
      <strong>Cross-platform test automation</strong><br><br>
      Using the testrunnerapp to manage 86box regression testing for serial ports against 6 OS releases with both A/B code branch test tracking
    </td>
    <td width="65%" align="center" valign="middle">
      <img src="https://github.com/user-attachments/assets/9d6943ad-b59f-4131-887f-f68373b4dd1b" width="400" alt="Test automation user interface">
    </td>
  </tr>
  <tr>
    <td width="35%" valign="middle">
      <strong>Python-defined test sequences</strong><br><br>
      Test lists are defined in Python files. Each test consists of individual steps that specify the actions and checks performed during automated execution.
    </td>
    <td width="65%" align="center" valign="middle">
      <img src="https://github.com/user-attachments/assets/7feb4fac-3030-447a-a611-a5b98bdf3c61" width="400" alt="Python test-list steps">
    </td>
  </tr>
  <tr>
    <td width="35%" valign="middle">
      <strong>HTML test reports</strong><br><br>
      Generates HTML reports documenting test execution, including individual step results, pass/fail criteria, and execution times.
    </td>
    <td width="65%" align="center" valign="middle">
      <img src="https://github.com/user-attachments/assets/c1a76834-ca4a-4d9c-9598-c4c44947a454" width="800" alt="HTML test execution report">
      <img width="800" alt="testrunnerapp_reportfail1" src="https://github.com/user-attachments/assets/a941a34d-e5d4-4388-9f3b-2fcb288ac870" />
    </td>
  </tr>
</table>




jj
j
j

