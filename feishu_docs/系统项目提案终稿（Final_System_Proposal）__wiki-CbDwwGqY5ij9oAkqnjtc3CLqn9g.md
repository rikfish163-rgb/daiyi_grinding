# 系统项目提案终稿（Final System Proposal）

New MATHCOUNTS Buzzer System

System Proposal

Created By:

Calvin Hamilton

Bhargav Nagalamadaka

Jacob Rast

Linus Wise

Clients:

Dr\. Leanne Holder

Jim Reppell

Date:

10/29/24

# Project Background and Introduction

MATHCOUNTS is a nationwide competition series for middle school students who are passionate about mathematics and problem\-solving\. In the Countdown Round, 2\-6 competitors are presented with a question and given 20\-60 seconds to answer\. Every competitor is provided with a button unit that is wired to a center console which resides in front of the competition operator\. The first competitor to buzz in responds to the question and, depending on the rules, either the second student to buzz in is allowed to answer or the rest of the students get another chance to buzz in\. Only the first buzzer pressed triggers a light and sound\. The competition operator can go to the next competitor who buzzed in, reset the button units, switch between operating modes, and manage the timers\.  A small timer will sit on the center console in sight of the competition operator and a larger one facing the competitors\. Both timers will display the same time and be controlled by the competition operator\. The competition operator manages the timers by starting, resetting, or changing the length of time that will be displayed \(as long as the timer isn’t currently ticking down\)\. For decades, Rose\-Hulman itself has used the same buzzer system, which currently relies on an extremely outdated computer\. Due to this, as well as the extensive wear on the system from its many years of operation, concerns have arisen that it may soon fail\. Therefore, our goal is to replicate the current system’s basic functions while modernizing it for durability and ease of use\.

The redesigned system, expected to last at least 10 years, will use an Arduino microcontroller interfacing with 6 button units, 2 timer displays, and a set of button inputs on the center console\. I2C communication will be implemented to reduce pin usage and ethernet cables will provide durable connections between components\. The device, which will take 5V as an operating voltage, will be powered via a standard 120VAC wall outlet, and custom PCBs will ensure a robust design, with ports for easy connection and disconnection of devices\. There will be two modes of the system set with a switch on the controller: Type 1 Mode \(reset boxes\) and Type 2 Mode \(next answer\)\. In Type 1 Mode, when a competitor presses their button first the operator will reset all buzzers, and if the answer is wrong, any competitor can buzz in again with a different answer\. In Type 2 Mode, the system remembers the order of all the buttons pressed, so when a competitor answers incorrectly, the operator can press the “Next Answer” button to go to the next competitor who buzzed in\. These two features will be operated by the same physical button, but its effect will change by flipping the “Change Mode” switch\. In both modes, the timer will not stop when a competitor buzzes in\. Additionally, by using “Timer Up” and “Timer Down” buttons, the length of the timespan of the timer can be changed by 5\-second increments, from 20 seconds to 60 seconds\.

Figure 1: Mockup of the Final Product

# Project Deliverables

Provide a system that meets the requirements of client\-provided MATHCOUNTS Buzzer System – Type 1 \& 2 Combined Equipment specification, Revision 1\.0, dated 10/19/2024, and includes:

- A functional power supply for the controller using a wall outlet

- Six pushbutton boxes that are connected using ethernet to the controller

- A LED/light on each pushbutton box that lights up when a competitor presses the button

- A seven\-segment display that shows the competitors the remaining time

- A main controller that interfaces between the pushbuttons and the operator\(s\), which includes:

- “Timer Up” and “Timer Down” buttons for setting the start time of the timer to 	anywhere between 20 and 60 seconds in 5\-second increments

- A "Reset Timer \+ Boxes” button for resetting the timer and the six pushbutton boxes

- A “Start Timer” button for starting the time

- A “Reset Boxes/Next Answer button that resets the pushbutton boxes and either does or does not go to the next person to answer based on mode

- A "Change Mode” switch to switch between two different modes of the system

- A buzzer that plays when time runs out or a competitor pushes their button \(three sounds total—one for timeout, two for buzz\-in\)

- A seven\-segment display on the operator’s controller to show them the remaining time

# Stakeholders

Table 1: Stakeholders Involved with Descriptions

|Stakeholder Name|Associated Features|Describe the stakeholders with respect to the Associated Features|
|---|---|---|
|Organizers|Affordable<br>Durable<br>Transportable|The organizers are the people who organize and run the competition\. They want the system to be affordable, since the budget is limited\. The system will be used for at least a decade, so it must be durable enough to last throughout that time\. Additionally, the system must be able easily to be transportable and survive normal use\.|
|Operators|Intuitive<br>Controller<br>Timer Unit|Operators are the people on the controller controlling the entire system\. They want the system to be durable and easy to use\. They prefer that the buttons on the controller be clearly labelled and easy to read\.|
|Competitors|Intuitive<br>Button Boxes<br>Timer Unit|The competitors are the stakeholders competing in MATHCOUNTS\. They will use a button box to buzz in and can see a timer unit that displays how much time is left to buzz in\.|
|Audience|Intuitive<br>Button Boxes<br>Timer Unit|The audience are the people who are watching the competition\. They want to focus on the competition, such as who buzzed in to answer the question\. The system must be accurate and responsive to run the competition correctly\.|

# Features

Table 2: Features Included with Descriptions

|Feature Name|Associated Stakeholders|Describe each Feature in specific terms with respect to what the stakeholders want and how you might measure it|
|---|---|---|
|Affordable|Organizers|The organizers would prefer that the system not be overly expensive\. The unit cost \(how much a copy of the final version would cost to build\) is limited to $200, and the development cost \(additional costs of tested components, outdated PCBs, etc\.\) is $300\. $500 total\. We hope to keep this much lower, however\.|
|Durable|Operators, Organizers, Competitors|The organizers and operators will be using this system for at least a decade, so the system should not suffer fatal mechanical or electrical failures due to years of reuse or from being in transit, which will be simulated by shaking the center console and individual button boxes for 1 minute and determining whether they are still functional\. The buttons must also not break from pressed 100 times in quick succession \(0\.5 seconds between each press\)\. The button boxes must not break when dropped from a height of three inches either\.|
|Intuitive|Operators, Competitors, Audience|The operators and competitors are all focused on the competition and do not want to struggle to understand the system's operation\. The system should therefore be designed such that even someone who has not seen the system before will be able to operate it after a brief explanation\. Each button box must have one button and an LED that must be bright enough and large enough to be immediately visible from the audience \(50 feet\)\. The buzzers must be audible enough to be heard from a large area \(between 60 and 80 decibels\)\. The buttons/dials on the center controller must be clearly labeled with letters that are at least 0\.3in tall\.|
|Transportable|Organizers|The system must be transported between competitions at different locations, so it must be easily transported by car\. As previously mentioned, this will be verified by shaking the center console and individual button boxes for one minute and making sure they are still functional\. We will be aiming for a weight, not counting cables, of under 16lbs\. The entire system, excluding wires must be able to fit inside a two\-gallon bag, and the wires must be easily coiled\.|
|Button Boxes|Competitors|Up to 6 boxes will be able to operate simultaneously\. The boxes will feature a button and an LED that will turn on when a competitor has buzzed in\. The lights on the other competitors’ boxes must remain off after the first competitor has buzzed in and must remain as such until the operator resets the system\. The LED must light up within 20ms of pressing the button\. The boxes will contain a single ethernet port that will allow them to be connected to the main controller\. A cable that is at least 25 feet long can be connected to this port\.|
|Controller|Operators|The controller will have a button for the operator to start the timer, a button to either reset the buzzers without resetting the timer or advance to the next player to buzz in, and a button to reset the buzzers and the time, all clearly labeled with 0\.5in labels\. The controller must contain its own timer unit and a mechanism to produce the buzzer sounds, of which there will be three distinct, audible tones \(two for buzzing in, one for a timeout\), all between 220 and 1480 Hz and 50 and 80 decibels\. The two timers must update within 10ms of each other\. The controller must also contain 7 ethernet ports to allow connection to the button boxes and timer unit\. The controller must contain a microcontroller that handles all the computing and logic required to run the system\.|
|Timer Unit|Operators, Competitors|The competitor\-facing timer unit must be at least 3 inches tall for the operator and competitors to see while competing\. The operator\-facing timer unit must be at least 1 inch tall\. The timer units must also be able to be reset, have their timespan increased or decreased by 5s \(with a maximum at 60s and a minimum at 20s\), and start when the operator presses those respective buttons, updating the timer displays within 10ms of being pressed\.|
|Ethernet cables|Operators, Competitors|Ethernet cables will be used to connect electronic components and send signals from the controller to the button boxes\. IEEE 802\.3 is a standard which shows that Ethernet cable can be used for wires to connect electronic components\. The cables will be at least 25 feet long and easy to coil\. In addition, the cables must be replaceable, meaning they can be unplugged from both the center console and the individual button boxes\.|

Table 3: Feature Validations and Verification Plans

|Feature|ID|Requirements|We need this requirement because…\(Validation\)|We will test this feature in our prototype by \(Verification\)|
|---|---|---|---|---|
|Affordable|A1\.0|The final version of the system should cost less than $200 to construct and development costs beyond that should not exceed an additional $300\. Our goal is for this to be much lower\.|An ideal system should always balance cost\-effectiveness with functionality, and ours will be no different\.|Collecting proper data to create a BOM table and see how much everything has cost \(as well as just keeping a running tally of how much we’re spending\)\.|
|Durable|D1\.0|The system should continue working after 100 button presses on each button press with a \.5 second gap between presses\. The buzzers shouldn’t break if dropped from a height of three feet and the controller should still function after being shaken for a minute\. Cables should be able to get stepped on without significant deformation\.|The whole point of this project is to create a more durable replacement for an old system, so creating one that would break down after a year would be pointless\. We want the system to last for another decade, so we should simulate a similar amount or greater level of usage\. Additionally, due to the buzzers being operated by children, we shouldn’t design something that will break if dropped accidentally from a table\.|We won’t be able to see how well the system holds up over an actual decade, so to replicate this, we’ll try buzzing in approximately a hundred times with each button box, as well as turning the system on/off a few dozen times to replicate this\. We’ll also drop the buzzers from 3 inches above the ground in a variety of orientations to verify that the system can handle light impacts without impacting functionality\.|
|Intuitive|I1\.0|The buzzers should have obvious functionality to the competitors, the controller should have obvious functionality to the operator, and it should be immediately apparent who has buzzed in first\. This can be verified through observation\. Additionally, the mode the controller is currently set to will be clearly indicated with a 0\.3in label on either side\.|In a competition of students’ math skills, it would be a shame if they couldn’t answer quickly enough because the system was too difficult to operate\. Additionally, in a competition, it will only cause headaches if it isn’t clear who buzzed in first\.|We’ll write a simple 1–2\-page instructional guide and have our client \(and possibly one of the professors who has operated the system in the past\) look it over to verify that everything makes sense\.<br>Additionally, if we have the time, we’ll aim to color code buzzers and have the center console light up in that color when that buzzer is pressed to make things more visually appealing\.|
|Transpor\-table|T1\.0|The entire system \(excluding cables\) should be of a reasonable size\. The main unit should be less than 12” by 14” by 5” and should be less than 15 pounds\. The button units should be less than 2 pounds each and no bigger than 2’’ x 3’’ x 1\.5’’\.|We don’t want to unnecessarily encumber the person setting up the system and considering other commercially available game show buzzers are light; this should be something we strive for as well\.|We will test that the system is easy to carry by carrying ourselves around campus for about 10 minutes, making sure it isn’t too heavy and that nothing falls apart when carried\. We will also shake it for one minute to make sure it will still be operational after driving on any rough roads\. We will also measure the size and weight to make sure they are below the limit\.|
|Functional|F1\.0|Dependably activates the correct button box’s buzzer and light for the length of a competition|Lighting up the wrong competitor’s button unit would be bad for a competition\.|We will test this by playing a game with the system ourselves for a 1\-hour span and making sure that the wrong buzzer never sounds off/wrong LED never lights up\. We will test buzzing in when another competitor is currently answering in both modes\.|
|Button Boxes|B1\.0|6 button boxes will be created that can be used simultaneously\. Each box includes an ethernet connection, a push button and an LED\. When the button is pushed, an output signal should be sent\. The controller can change the LED status at any time\.|These devices are what allow the competitors to enter their answers and will act as the primary source of inputs for the main controller|We will test that the controller receives and handles the inputs within 10ms, not accepting any other inputs that may occur after that timeframe\. The button rise time should be under 2 ms\.|
|Controller|C1\.0|The controller is the center unit that will allow the operator to manage the competition\. It will be able to accept inputs from up to 6 button boxes, will be able to support the concurrent use of all boxes and the timer, and will handle built\-in control inputs within 20ms \(defined below in interactions model\)\.|The controller will be the center control unit for the system\. This will include an Arduino microcontroller to handle inputs and outputs and an interface for all connected devices\.|We will test that it takes less than 20ms from receiving a signal to sending an output signal to the proper LED\. We will ensure that simultaneous presses are handled gracefully by making sure that only one button box input is designated as the first\.|
|Timer Unit|U1\.0|The timer unit shows the competitors the amount of time left to answer the question\. It should represent an accurate time to a resolution of 20ms|This is used by the students to determine how much time they have left to answer\.|The timer will display the correct time remaining to within 20ms\.|

# Black Box Model

Figure 2: Black Box Model of the System

# Actors, Inputs, Outputs

Table 4: Actors and Input/Output Descriptions

|Actor Name|Description|I/Os|
|---|---|---|
|Wired power supply|A power supply connected to a wall outlet that will provide enough power for the entire system\.|Should be able to take 120VAC and output a consistent, safe voltage of ?VDC and ?A into the microcontroller and PCB inside the center console\.|
|Center Controller|Controls the timer, buzzer, and push button boxes\.|Control Signals \(“Reset Timer”, “Reset Timer \+ Boxes”, “Reset Boxes/Next Answer”, “Switch Mode”, “Timer Up”, “Timer Down”, and “Start Timer”\), all of which take digital input\.<br>Sound outputs \(3 distinct sounds, between 20 and 1480 Hz and 50\-80 decibels\)\.<br>5VDC, ?A power input\.<br>Smaller timer display output on the center console itself \(at least 1 inch tall\)\.|
|Light Output|LEDs or other devices should be able to display light to the competitors and the audience\.|Take in 3\.3V and ?A, output at sufficient brightness to be seen clearly 15ft away\.<br>Timer should display correct time to 20ms\.|
|Sound Output|A buzzer should play a sound output that makes a noise when a competitor presses their button or the timer runs out\.|Take in ?V and ?A, output three distinct sounds between 20 and 1480Hz and 50\-80 decibels\.|
|Competitor|Pushes the push button when they want to answer a question\.|Output 6 digital inputs \(1 for each competitor\) into system\.|
|Operator|Controls the system using the controller|Output 4 digital inputs and 1 analog input \(knob\) into system\.|

# Interactions Model

Table 5: Interactions with Descriptions

|Interaction|Description|
|---|---|
|Mode Selection Switch is positioned|The system will operate in either the Type 1 or Type 2 operating mode depending on the position of this switch\.|
|Timer Up or Timer Down button is pressed|The maximum time allowed for answers is increased or decreased by 5s, respectively\. This is reflected on the timer displays\. These values will not go above 60s or below 20s and will start at 60s\. Pressing these buttons will not affect the timer while it is running \(while students are answering a question\)—they will only affect the timer after Reset Timer has been pressed but before Start Timer has been \(or just when the system is first powered on, before Start Timer has been pressed for the first time\)\.|
|Start Timer button pressed|The timer starts counting down from the set maximum time within 20 ms of the actual time elapsed\.  When it reaches 0, it stops counting down and deactivates all buttons \(but not lights\)\.|
|Competitor button pressed|If this is the first button pressed, the corresponding light illuminates and the buzzer sounds\.<br>In Type 1 Mode, the buttons of all other competitors are deactivated\.<br>In Type 2 Mode, buttons pressed by all other competitors will be recorded in the order received\.|
|Buzzer Reset / Next Answer button pressed|Type 1 Mode: The light of the responding competitor will go out within 20 ms, and all competitor buttons will be reactivated\.<br>Type 2 Mode: The light of the responding competitor will go out within 20 ms, and the light of the next competitor to press his/her button will be illuminated\.  The button of the previously answering competitor will be deactivated\.|
|Timer Reset button is pressed|In both modes, all lights are turned off, all buttons are reactivated, and the timer is reset to its selected starting value\.  This is all completed within 20 ms|

# Budget and Purchase Plan

Purchase Plan:

Part orders for the project will be from one of two avenues\. Most parts will be ordered through the Rose\-Hulman ECE Department and the balance will be accumulated to be paid at the end of the project, Rose\-Hulman may be subsidizing a portion of this balance, subject to a final decision by the ECE department\. Some parts will be directly ordered by the development team\. In these cases, receipts will be accumulated, and the balance and log of receipts will be sent to the clients at the end of each quarter\. Billing information will be provided upon sending the balance\.

Table 6: Preliminary Budget breakdown for tentative project milestone

Note: The Unit Cost Budget is the cost of the parts used in the final product\. The Development Budget is the cost of everything else, including other tested components\. This is a very tentative budget, and specifics

|Milestone|Description of Resources Needed|Estimated Unit Cost Budget|Estimated Development Budget|
|---|---|---|---|
|All parts decided on and ordered|2 3\-inch 7\-segment displays \(2\-digit numbers only, so need 2\)<br>2 1\-inch 7\-segment displays<br>7 25\-foot ethernet cables<br>14 ethernet cable ports<br>1 Arduino Nano 33 board<br>1 pin extender<br>6 button unit buttons<br>5 center console buttons \(Reset Timer \+ Boxes, Start Timer, Timer Up, Timer Down, and Next Answer/Reset Boxes\)<br>1 center console switch \(Switch Mode\)<br>6 LEDs<br>12\+ resistors \(for buzzers and LEDS, values TBD\)<br>A good number of wires to connect parts together \(no point in setting a specific number\)<br>1 120VAC\-5VDC outlet\-micro USB cable|Proposed lump sum: $150 \(including cost of all electrical components, such as the ethernet cables and microcontroller boards\. Ideally, we won’t need to spend the whole amount\)\.|Proposed lump sum: $270<br>\(to cover experimentation or breakage with resistors, LEDs, microcontrollers, etc\.\)\.|
|Complete functionality using breadboard|Any parts we needed to replace/didn’t realize we needed|Proposed lump sum: $30 \(to cover last minute adjustments to meet verification requirements\)|$0|
|PCB produced and functional|Cost of milling, per unit will produce 6 small pcb’s for button boxes, and a large PCB for the controller|Proposed lump sum: $20 \(This is a very rough estimate of the on\-campus PCB mill, depending on complexity or size it could be higher or lower\)|Proposed lump sum: $20 \(for potential first draft of final design\)|
|3D\-printed parts created, durable, and functional|Costs of 3D\-printing, if there are any \(we’ll be using Rose\-Hulman’s 3D printed and engraving technology\)|$0|$0|
|Final completion|Posterboard for Rose Show|$0|Proposed lump sum: $10 \(for cost of printing\)|
||Total|$200|$300|

# Signatures and Agreement

The purpose of this document is to serve as an agreement between the client and the student team about the scope of the project and the final deliverables\. By signing below, the client is agreeing with the students’ understanding of this project as described by the various models and expected deliverables\. The students should take some time to help the client understand the types of information that is contained in these models before requesting a signature\. The Supervisor’s signature is an acknowledgement that this project scope and deliverables are sufficient for continuing with project development and that they are agreed upon by both the students and client\. Significant changes to the scope of this project in terms of Features, Metrics, and/or Deliverables will require approval with a new set of signatures\. Minor changes such as updates to the various models in order to better describe the currently approved Deliverables and project scope do not require approval\.

Client: \_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

Supervisor: \_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

Team Member 1: Calvin Hamilton 10/2/24

Team Member 2: Bhargav Nagalamadaka 10/2/24

Team Member 3: Jacob Rast 10/2/24

Team Member 4: Linus Wise 10/2/24

> （注：部分内容由豆包工作 AI 生成）
