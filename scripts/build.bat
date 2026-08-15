@echo off
REM ============================================
REM  Addled - Build Pipeline
REM  Wrapper for the canonical build.bat in the
REM  repo root (deps + dashboard + installer).
REM ============================================
call "%~dp0..\build.bat"
