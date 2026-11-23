@echo off
setlocal EnableExtensions
set "FORGE_SCRIPT=%~dp0forge"

if defined FORGE_BASH (
  if exist "%FORGE_BASH%" (
    set "BASH_EXE=%FORGE_BASH%"
    goto :run
  )
)

for %%P in (
  "%ProgramFiles%\Git\bin\bash.exe"
  "%ProgramFiles(x86)%\Git\bin\bash.exe"
  "%LocalAppData%\Programs\Git\bin\bash.exe"
) do (
  if exist "%%~P" (
    set "BASH_EXE=%%~P"
    goto :run
  )
)

for %%I in (bash.exe) do (
  if not "%%~$PATH:I"=="" call :check_bash "%%~$PATH:I" && goto :run
)

for %%I in (git.exe) do (
  if not "%%~$PATH:I"=="" (
    if exist "%%~dp$PATH:I..\bin\bash.exe" (
      set "BASH_EXE=%%~dp$PATH:I..\bin\bash.exe"
      goto :run
    )
    if exist "%%~dp$PATH:I..\usr\bin\bash.exe" (
      set "BASH_EXE=%%~dp$PATH:I..\usr\bin\bash.exe"
      goto :run
    )
  )
)

echo forge: Git Bash not found. Install Git for Windows or set FORGE_BASH. 1>&2
exit /b 127

:check_bash
set "CAND=%~1"
set "TEST=%CAND:\System32\bash.exe=%"
if not "%TEST%"=="%CAND%" exit /b 1
set "TEST=%CAND:\SysWOW64\bash.exe=%"
if not "%TEST%"=="%CAND%" exit /b 1
set "TEST=%CAND:\WindowsApps\=%"
if not "%TEST%"=="%CAND%" exit /b 1
set "BASH_EXE=%CAND%"
exit /b 0

:run
"%BASH_EXE%" "%FORGE_SCRIPT%" %*
exit /b %ERRORLEVEL%
