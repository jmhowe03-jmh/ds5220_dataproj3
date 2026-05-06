# ds5220 Data Project 3: Weather Tracker 
building off project 2
## vxx4kn Jillian Howe
In this project we took live weather data from my hometown of Syracuse, NY and made an interactive app that can be accessed via the classes discord chat bot. When requested the app gives information drawn from current or recent temperature, humidity, and wind currently available. Compared to project 2, we updated the code so that the temperature is in the current units (Farenheit rather than celcius). Additional resources were adding to compare readings from ~24 hours ago, and to flag any anomalies in the data. If I were to add onto this project I would make sure to change the time from UTC to local EST time, or include both in my plot. Additionally, adding a second weather tracker for Charlottesville would be interesting to compare the two in real time.

I originally tried to make the IAM policy in AWS however I kept running into errors so I added the pollicy-dev.json within chalice to mitigate this result. I also had some difficulties using chalice as my windows computer can be difficult too run packages within the commandline, and I recenly switched to using the UVA one drive which complicated the paths to use python and conda. I switched to a virtual environment and local directory which appeared to solve the issues I was running into. Additionally I orignally tried to upload the app and requirements dependencies directly to AWS lambda without chalice and this caused many issues.

## Overview
This project is a serverless weather tracking system that collects hourly weather data for Syracuse, NY using AWS Lambda, DynamoDB, and S3. An EventBridge-scheduled Lambda fetches temperature, humidity, and wind speed from the Open-Meteo API every hour and stores timestamped records in DynamoDB. A Chalice-built REST API exposes five endpoints including current conditions, trend analysis, a 24-hour comparison, anomaly detection, and a live plot chart. The whole system runs without any standing infrastructure and is registered with the course Discord bot.

PROJECT API URL: https://zwyevtpojg.execute-api.us-east-1.amazonaws.com/api/


Open-Meteo Weather API — fetch hourly temperature, wind speed, precipitation, or cloud cover for any lat/lon without an API key. https://open-meteo.com/en/docs

## Deliverables
A deployed ingestion pipeline (Event/Timer + Lambda + Database/Storage) that has been running long enough to have collected real data.

A deployed Chalice API with at least three resources, registered with the course bot.

A short README in your project repo covering:

What data source you tracked and why.
How often it's sampled and what the storage schema looks like.
A description of each API resource and what it returns.
Any stretch goals you added.
Submit your repo URL in Canvas for grading.
