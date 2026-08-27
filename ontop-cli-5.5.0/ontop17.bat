@echo off
rem
rem ONTOP WRAPPER: force Ontop to use JDK 17 without touching the system Java 8 setup.
rem Usage is identical to ontop.bat, e.g.: ontop17.bat help
rem
set "JAVA_HOME=D:\Develop\java\jdk-17.0.20+8"
call "%~dp0ontop.bat" %*
