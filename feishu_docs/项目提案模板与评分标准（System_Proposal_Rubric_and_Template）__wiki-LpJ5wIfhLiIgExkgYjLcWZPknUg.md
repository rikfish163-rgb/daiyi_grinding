# 项目提案模板与评分标准（System Proposal Rubric and Template）

Description: How do you know when your project is “done” or if it is a “success”? Ultimately, these questions are answered by how well you meet a set of stakeholders’ needs\. This is not always easy to do because often the client is only a small subset of all the stakeholders and not everyone speaks the same language\. While the engineer understands things like filters, embedded system design, and electromagnetic physics, the client or stakeholder may just want something that is “easy to use” and “lasts a long time on a single battery charge”\. Simply understanding what the client truly wants and then beginning to translate that into an effective solution is no small task\. This first major assignment in Senior Design is set up to do just that\. You will be spending the first few weeks of the course working with the client to translate the client’s needs into a high\-level engineering description of your project\. You will then use this engineering description to guide your team towards a solution that should meet all of the client’s needs\.

An exemplarary System Proposal will include the following items:

- An executive summary that describes the goals of the project and why the project is important

- Deliverables that describe what the client wants from his/her perspective

- A Domain Model that adequately describes the Actors on and Interfaces to the system and the interactions between the Actors and the system

- A Stakeholder Model that adequately describes the major stakeholders and the desired features

- A System Modes Model that adequately describes the various system modes and how the system changes modes due to the various events that occur

- An initial Logical Architecture that is consistent with the other models, has the right level of detail, and shows some insight into the project

- Consistency between all of the different models

Grading will be based on the following rubrics:

|Item|Exemplary|Adequate|Inadequate|
|---|---|---|---|
|Title|The title is reasonable length and captures the purpose and/or outcome of the project|The title describes the purpose and/or outcome but could be shorter or more relevant|The purpose or outcome of the project cannot be gleaned from the title|
|Background and Introduction|A very clear description of the overall project goal and why the project is important\. Figures and references are used to illustrate the context of the problem|The overall goal can be discerned but could be more clear or concise\. Could use more figures or references to demonstrate context|The primary  goal cannot be discerned\. No figures or references are used\.|
|Deliverables|A very explicit and clear idea of what the client wants and how it will be used presented from the clients perspective in easy to understand language|The end result and how it will be used can be discerned with some effort|It is not clear what the end result will be\. The language is overly technical to get a layman’s perspective|
|Stakeholder Model|All known stakeholders and their desired features are accounted for\. Is easy to read and follow\.|All primary stakeholders and desired features are present\. Could be more organized or easier to read|Missing some key stakeholders or features\. Cannot discern nec info|
|Functional Architecture|The model accounts for all primary actors, I/Os, functions, and interactions\. Is clearly and adequately labeled and easy to read\. Is consistent with other models\. The model is sufficient to completely describe all stakeholder features\.|All major actors are accounted for, but could be better organized or easier to read\. Some minor lack in consistency\. Some behaviors may be difficult to see in the model\.|Primary actors, interfaces, and/or interactions  are missing\. Inconsistent or unclear\.|
|Interactions Model|Accounts for the actors and features and appropriately describes 3\-5 of  the primary interactions|Is still useful but either slightly too complicated or too simple|Missing key interactions\. Is too simple or overly complicated to be useful\.|

## Example project for systems\-engineering model templates

The following model templates are being provided to you as examples of what we’re looking for\. They are not exhaustive in scope, but have all of the necessary elements\. All that hard work you put into your Junior Design project is now going to pay off as a common experience on which you can build\.  Leave the red italicized text in the template and add your content below it for each section\. Once your content is complete for the final draft, delete the red italicized text\.

Figure 1: An image of the autonomous vehicle that you designed a power supply and controller for in Junior Design\.

# Project Title and Cover Page

Work with your supervisor and client to develop a meaningful title for your project\. This is not as trivial as it sounds\. You want to capture something of the technical detail of the work without the title being too long or cumbersome\. The title can change throughout the year in order to adjust for changes in understanding or scope of the project\. Place the title on a cover page with your names, date, …

# Project Background and Introduction

Provide background and context for your project\. We need to know why you’re doing this project in the first place\. What makes it important to the client and the stakeholders? What, if anything, has been done in this area by other people and why is that insufficient? What does the client hope to achieve by asking you to do this project? Provide an overview of the technology that you’ll be working with\. How does it work? What concepts is it based on? What hardware and/or software is involved? This section will be used in the Introduction of your final report in ECE462\.

# Project Deliverables

This section should provide a very high\-level description of the physical deliverables that the client wants to have in their hands at the end of the project as presented from the clients perspective\. You could include a bulleted list and then explain each bullet in more detail\. We are mainly concerned here about tangible artifacts that are designed and prototyped, not final summative documentation such as final reports\. However, if the client specifically wants extra documentation such as a user manual, that is a tangible artifact that should be listed\. Be as specific as possible\. This section will form the expectations of what the team should hand off to their client when the project is finished\. It should not be more technical than the client is easily capable of understanding\. Using the ECE362 project as an example, you could say that the deliverables are a

- A functional supercapacitor based power supply on a PCB that will attach and connect to the vehicle and

- Vehicle control software that will enable the vehicle to travel around both tracks autonomously

# Stakeholder Model

This is the first model that you should develop and must be done in collaboration with the client and/or other stakeholders\. The purpose of this model is to identify those people or groups of people who have a vested interest in the outcome of this project and the characteristics that those people or groups want the outcome to have\. Those people and groups are called Stakeholders and the characteristics that they want are called Features\.

The features are usually expressed in general terms with words or phrases that have a suffix such as –ility or –able, but not allways\. Some common examples are manufacturable, affordable, and maintainable\. At least one of the features must describe the primary purpose of the project\.

Because the Features are too broad or general to design from, you need to start decomposing them into Metrics or Attributes\. What we are calling attributes and features are often called stakeholder requirements in the systems engineering community\. Create a table that identifies which feature attributes and metrics are important to which stakeholders\. Each metric must be both validated at the beginning of the project and verified once the prototype is developed\. Remember that Validation is making sure that the Feature/metric/attribute is adding value for the appropriate stakeholder\. Verification is making sure your solution can achieve that metric\. You must include a Feature, metric, validation, or verification method that is based on each of the following topics:

- One or more of these metrics must be validated with respect to a recognized Standard \(such as IEEE 802\.11\), code \(such as NEC70 National Electrical Code\), or industry specification \(such as PCIe\)\.

- One or more Features and/or metrics must be related to one or more of the following broader factors: public health and wellfare, safety, global factors, economic factors, social factors, environmental factors\.

|Stakeholder Name|Associated Features|Describe the stakeholders with respect to the Associated Features|
|---|---|---|
|High\-School Student|Fun<br>Educational<br>Robust<br>Operational<br>Intuitive UI|The high\-school students are going to be the primary users of autonomous vehicle\. They want it to be fun in the sense that they won’t get frustrated because it is too difficult to learn\. They want it to be technologically relevant and feel like they are learning something useful\. The association of the vehicle with ECE technology might need to be made explicit because they may have no prior knowledge of the field\. The vehicle needs to work when the students put the vehicle on the track\. It should start up with little delay and the control algorithm should take the vehicle around the track at a decent speed without leaving the track too many times\. The power supply should recharge consistently and quickly without breaking or blowing capacitors\. The UI should be easy for the students to learn and provide useful feedback for debugging and for knowing the state of the vehicle\. At the end of the race students want to see how much energy they consumed while running the vehicle\.|
||||
||||

|Feature Name|Associated Stakeholders|Describe each Feature in specific terms with respect to what the stakeholders want and how you might measure it|
|---|---|---|
|Fun|High\-school students and teachers<br>Rose\-Hulman|The project should be Fun in the sense that it is interesting to the students and teachers\. The autonomous vehicle system should not have repeated failures and high\-school students should be able to get it up and running in a short amount of time\. We can measure this in several ways:<br>User testing before release where several uninformed users take the vehicle out of the box and get it to go around the track following our instructions\. Record number of failures and what happened\.<br>Survey Teachers for: a\) number of students interested in project at beginning vs\. after competition, b\) problems encountered when starting the project, and c\) how many students interested in ECE before and after project\.<br>Survey Rose Freshman to see number of students coming to Rose due to participating in this program\.|
||||
||||

Figure 2: Spreadsheets showing some of the Stakeholder and Feature definitions for the example project\.

|Feature|ID|Requirements|We need this requirement because…\(Validation\)|We will test this feature in our prototype by \(Verification\)|
|---|---|---|---|---|
|Energy Capacity|EC1\.0|The power supply will power the vehicle for 1\.5 minutes while it is maneuvering around either course|The ninety\-second limit provides for a reasonably sized power supply but also creates an interesting amount of time for a race\.|Installing our power supply, placing the vehicle on the track, and running the vehicle on our power supply\.|
|Energy Capacity|EC1\.1|The power supply will be provide at least a 1A continuous current draw to the vehicle for the duration in FP1\.0|||
|Versatility|V1\.0|The controller will drive the vehicle autonomously around both tracks for the duration specified in FP1\.0|This is the basic tenet of the competition\. Students learn to program and can see the immediate effects of their program as they make changes\. Also Autonomous vehicles are coming and this exposes students to some of the challenges\.|With the vehicle on the track, we will start it moving and observe if it deviates of the edge of the track|
|Versatility|V1\.1|The vehicle will stop automatically after \[the time duration specified in FP1\.0\] of going around the track\.|||
|Versatility|V1\.2|The vehicle will communicate with a laptop using a  Bluetooth connection|The Bluetooth standard uses less energy than WiFi for communication and has sufficient bit rates for our purposes|send command strings to the vehicle from a laptop and read data from sensors on the vehicle and send the data to the laptop|
|Ease of Use|EU1\.0|The controller should have a well defined API so that high\-school students can easily program the vehicle|The global demand for experts in ECE disciplines is increasing at a rapid pace\. The purpose of the project is to encourage high\-school students to enter ECE as a career\. If the vehicle is too difficult to program it may actually discourage them\.|Recruit volunteers to read the API documentation and write a simple program for the vehicle\. Give volunteers a survey to determine the effectiveness of the API for writing the program\.|

Figure 3: A map of stakeholders to feature attributes with Validation and Verification methods\. The text in blue is indicative of including standards and broader impact factors as Features\.

# Black Box Model

The Black Box Model defines the system boundary and the system behavior at a high level\. The point of this model is to try to capture the interactions of the system with the Actors and I/Os\. Remember that Actors and I/Os are nouns, not verbs\. You may go through several steps to get to something that looks like Figure 4 below, but we only want to see the final result\. Remember to include definitions for each of the Actors and I/Os in the form of a table or spreadsheet\. The definitions shown in Figure 5 are only a partial set to give you an idea of what we’re looking for\. Your document should be complete in describing all Actors, I/Os, and Functions\.

You should aim to have somewhere less than 10 Actors and less than 10 functional blocks\. Anything more than that and the model has too much detail to be useful at this level\.

Figure 4: Black Box Model for the HSAV competition vehicle\.

|Actor Name|Description|I/Os|
|---|---|---|
|Wired or wireless charger|The chargers that will be used to charge up the supercapacitor power supply|Charging Energy|
|Camera|The onboard camera that senses the line in the center of the track\.|Line data|
|Vehicle Controller|All electronics on the vehicle that are powered by the power supply\. This includes the ESP32 board, camera, and servo controllers\. This also represents the ESP32 device as a producer of control signals and sensor processor\.|Energy<br>Control Signals|

Figure 5: Definitions of some of the Actors and I/Os from the Black Box model\.

# Interactions Model

The Interactions Model describes your system from the perspective of how it interacts with the various Actors\. Interactions should be named as verbs followed by a noun and have the same format…

Start with an Actor

Describe the input coming from the Actor

Describe how the system should respond to the input

Describe any outputs from the system to Actors

One way to start coming up with interactions is thinking about a user manual for your system; another could be to use the functional architecture and think about each actor; yet another could be to use sequencing diagrams as shown below in Figure 7\. In any case, don’t forget to include the primary purpose\(s\) of your project in this list of interactions\. You might find that you come up with an interaction for which you have not provided an actor in the Functional Architecture or vice versa\. This means you need to continue revising your models until you reach a steady\-state\. Ultimately, the success of your project is determined by how well it addresses the Stakeholder Features\.

|Interaction|Description|
|---|---|
|Starting a race|When the user pushes a button on the ESP32 board the vehicle software system will interpret this input as the start signal\. The software will then immediately ramp up power to the wheels, set the steering angle, initiate reading data from the camera, start the 90 second countdown timer, and begin logging energy use\.|

Figure 6: Illustration of the desired format for the Interaction Model\.

# Final Signatures and Agreement

The purpose of this document is to serve as an agreement between the client and the student team about the scope of the project and the final deliverables\. By signing below, the client is agreeing with the students’ understanding of this project as described by the various models and expected deliverables\. The students should take some time to help the client understand the types of information that is contained in these models before requesting a signature\. The Supervisor’s signature is an acknowledgement that this project scope and deliverables are sufficient for continuing with project development and that they are agreed upon by both the students and client\. Significant changes to the scope of this project in terms of Features, Metrics, and/or Deliverables will require approval with a new set of signatures\. Minor changes such as updates to the various models in order to better describe the currently approved Deliverables and project scope do not require approval\.

Client:\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

Supervisor: \_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

Team Member 1: \_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

Team Member 2: \_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

Team Member 3: \_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

Team Member 4: \_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

> （注：部分内容由豆包工作 AI 生成）
