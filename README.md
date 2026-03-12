# Feature Manifest Differences
In the Actions tab the Manifestor Deep Diff action can be manually run to find any differences in the FeatureManifest.yaml file. The repository stores a snapshot of the page after running and it will always compare the latest version with the last snapshot.

<img width="1333" height="460" alt="image" src="https://github.com/user-attachments/assets/1d4f6c0d-78c9-4633-9372-da9a35fca0c8" />

Any differences found will be uploaded as issues assigned to a milestone for tracking purposes. Issues should be closed once the change was tested.

<img width="609" height="376" alt="image" src="https://github.com/user-attachments/assets/cab4e12c-c316-41e5-ab6c-0d5b3c65f61d" />

A .json with the differences will also be available as an artefact after the script finishes running.
The archive of differences can also be found: [here](https://drive.google.com/drive/folders/19h9LueDL9gUYSkxYBI6FT2ZhirD-7avK?usp=sharing)

<img width="1736" height="878" alt="Diff output" src="https://github.com/user-attachments/assets/3b95b196-70d8-40af-beaa-3b3f0bde589f" />



# Tracking Feature Config testing outside of auto-created issues
Note: Do not create a tracking issue for experiments/rollouts where only filters were validated.

After a Feature was tested as a Feature Config or part of Experiment/Rollout validation, an issue should be logged for tracking purposes:
1. Create a new issue using the "Feature Tested" template.
2. Add a link to the QA JIRA ticket where the feature/experiment was tested.
3. Add the feature config name in the title and milestone section.

Once testing is finished:
1. Add the date when the Sign-off was sent.
2. Add labels with the tested Firefox Versions
3. Close the issue and select a QA status color in the Projects section
   
<img width="1375" height="652" alt="image" src="https://github.com/user-attachments/assets/33383ed7-cc4d-43d0-9bb3-cb7bf59cbe45" />

# Project Tracking board
All of the issues logged for every Feature Config can be found in the Project section along with the QA Status and tested versions.

<img width="1397" height="682" alt="image" src="https://github.com/user-attachments/assets/a9a28d3a-2cf7-4055-9731-39234c8c72ed" />
