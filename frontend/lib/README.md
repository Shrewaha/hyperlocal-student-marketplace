# Dependencies

Put `json-20231013.jar` here.

Download it from Maven Central:
https://repo1.maven.org/maven2/org/json/json/20231013/json-20231013.jar

The jar is deliberately not committed — `.gitignore` keeps binaries out of the
repository. Anyone cloning this repo downloads it once with the link above.

The JavaFX SDK is separate: get it from https://openjfx.io and point the
`--module-path` VM option at its `lib` folder.
