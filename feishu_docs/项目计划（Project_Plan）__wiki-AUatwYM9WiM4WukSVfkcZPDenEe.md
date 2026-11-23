# 项目计划（Project Plan）

New MATHCOUNTS Buzzer System

Project Plan

Created By:

Calvin Hamilton

Bhargav Nagalamadaka

Jacob Rast

Linus Wise

Clients:

Dr\. Leanne Holder

Jim Reppell

Date:

10/6/24

# Executive Summary

MATHCOUNTS is a nationwide competition series for middle school students who are passionate about mathematics and problem\-solving\. In the final round, 2\-6 students are presented with a question and given 20\-60 seconds to answer\. The first student to buzz in responds and, depending on the rules, either the second student to buzz in is allowed to answer or the rest of the students get another chance\. Only the first buzzer pressed triggers a light and sound\. The competition operator, sitting at the center console to which all the other button units are wired, can light up the button units whose button was pressed after the current one, reset the button units, switch between operating modes, and manage the timers \(a small one on the center console itself and a larger one facing the contestants, both of which keep the same time\)\. They can do this by starting, resetting, or changing the length of time that will be displayed \(as long as the timer isn’t currently ticking down\)\. For decades, Rose\-Hulman itself has used the same buzzer system, which currently relies on an extremely outdated computer\. Due to this, as well as the extensive wear on the system from its many years of operation, concerns have arisen that it may soon fail\. Therefore, our goal is to replicate the current system’s basic functions while modernizing it for durability and ease of use\.

The redesigned system, expected to last at least 10 years, will use an Arduino microcontroller interfacing with 6 button units, 2 timer displays, and a set of button inputs on the center console\. I2C communication will be implemented to reduce pin usage and ethernet cables will provide durable connections between components\. The device, which will take 5V as an operating voltage, will be powered via a standard 120VAC wall outlet, and custom PCBs will ensure a robust design, with ports for easy connection and disconnection of devices\. There will be two modes of the system set with a switch on the controller: Type 1 Mode \(reset boxes\) and Type 2 Mode \(next answer\)\. In Type 1 Mode, when a contestant presses their button first the operator will reset all buzzers, and if the answer is wrong, any contestant can buzz in again with a different answer\. In Type 2 Mode, the system remembers the order of all the buttons pressed, so when a contestant gets a question wrong, the operator can press the “Next Answer” button to go to the next contestant who buzzed in\. These two will be the same button physically, but its effect will change by flipping the “Change Mode” switch\. In both modes, the timer will not stop when a contestant buzzes in\. Additionally, by using “Timer Up” and “Timer Down” buttons, the length of the timespan the timer runs for can be changed in 5\-second increments, from 20 seconds to 60 seconds\.

# Timeline

# Major Milestones

Table 1: Milestones with Descriptions and Due Dates

|Milestone Name|Description|Verified by|Date of Comp|
|---|---|---|---|
|All parts decided on and ordered|Specifics of power supply, cables, microcontroller, pin extenders, buttons, knob, wires, buzzers, lights, and 7\-segment displays will be worked out at this point\. Everything should be ordered through the client\.|It should be verified that the total cost is a good bit below $200 \(in case replacements are needed\) by calculating the total cost and keeping a BOM table\.|10/13/24|
|Basic buzzer system functionality using breadboard|The basic functionality of a game show system—where the first to press a button is the only one whose LED lights up and buzzer goes off—should work at this point\. Most of the code should be completed at this point\. The only functions that will need to be implemented will be the contestants’ buttons and the buzzer reset\. The system should work with 2\-6 buzzers\. Only needs to work on a breadboard and using a DC power supply at this point, but the different units should be connected using ethernet cables\.|Should verify that all buttons, lights, and buzzers work through observation\. Additionally, we will verify the power needed and make sure only the first button unit’s LED and buzzer go off\.|11/8/24|
|Basic buzzer system \+ timer functionality with breadboard|Same as above, but now with both timer units built in and functional, as well as the timer reset, timer up/down, and start buttons\.|Should verify that functions work as intended and that the timers show the same time\.|11/16/24|
|Complete functionality using breadboard|The mode\-switching button and next answer button will be implemented at this point\. The code will be completed, and all signal times laid out in the system proposal should be achieved\. At this point, the system should work as it would in the final product\. Also, at this point the system will be powered by the designated power cord\.|Verify the signal times laid out in the system proposal using an oscilloscope\. Also, play a few games to make sure everything works right, and check that the system still works after a couple hours of use\.|12/10/24|
|PCB produced and functional|Same as above, except now the system works with a PCB as the center unit\.|Verify the same signals and durability as on a breadboard\.|PCB ordered on 12/20/24, implemented 1/15/25|
|3D\-printed parts created, durable, and functional|3D\-printed parts will be designed \(with clear text, as laid out in the system proposal\) but will have the parts placed inside them yet\. Everything should be comfortable, stable, and should not break easily\.|As mentioned in the system proposal, we will shake the system to test its durability\. We will also measure the size, weight, and transportability of everything\.|1/15/25|
|Final completion|Parts will be placed inside the 3D\-printed units, and functionality of everything will be verified one more time\.|We will play another game, this time ideally in Hatfield Hall, to make sure everything works as intended and that the button units do not break during normal play\.|2/1/25|

# Division of Labor

Table 2: Description of Task Division

|Team Member|General Roles and Responsibilities|
|---|---|
|Jacob Rast|Will be responsible for PCB design, designing non\-timer code \(including ensuring only the first button pressed will respond\), “Reset Timer \+ Boxes” button, mode\-switching operations, and writing instruction guide\.|
|Bhargav Nagalamadaka|Will be responsible for LEDs and buzzers, 3D printed component construction \(including button placement and feel\), and achieving desired signal speeds as laid out in system proposal\.|
|Calvin Hamilton|Will be responsible for center console circuit construction \(including pin extensions and ensuring proper power\) and general team management \(including scheduling and contact with clients\)\. General team review and revision, will help with component integration\.|
|Linus Wise|Will be responsible for everything to do with timer units \(including timer reset button, timer start button, timespan up/down buttons, timer\-handling code, and time accuracy\) and cables \(including ports and durability\)\.|

Table 3: Descriptions of Which Tasks Can Be Completed in Parallel

|Milestone|Parallel Activities and How to Allocate Labor|
|---|---|
|PCB produced and functional|These are mostly independent tasks\. Jacob will be responsible for designing the PCB, and as Linus will be free, he can help with verification\.|
|3D\-printed parts created, durable, and functional|Bhargav will be responsible for designing the 3D printed components, and Calvin will help make sure everything about the center console and button units still functions when not on a breadboard\. The two should also test durability, comfort, and clarity\.|

|Basic buzzer system \+ timer functionality with breadboard|Jacob, Bhargav, and Calvin can focus on the non\-timer components of the system\. Linus should make sure the cables are properly sending information, though\.|
|---|---|
|Basic buzzer system \+ timer functionality with breadboard|A lot of the timer’s functionality, such as ticking down and resetting to a variable time, can be implemented separately from the rest of the system, and Linus should be able to focus on these early on\. Ideally, we’ll mostly all work together at this early stage, though, to make sure we’re all on the same page, and once the parts of the rest of the system that do affect the timer are implemented, we’ll all work on those together as well\. Also, Calvin should make sure the timer units will get enough power|

Table 4: Descriptions of Potential Roadblocks or Bottlenecks

|Milestone|Bottlenecks and Possible Solutions|
|---|---|
|Complete functionality using breadboard|This must be completed before it can be moved to a PCB, and we should also be confident in the parts we’re ordering before we start creating the 3D prints\. There’s not much way around this, but this is the main thrust of the project, anyway\.|

|Milestone|Bottlenecks and Possible Solutions|
|---|---|
|All parts decided on and ordered|We need the parts before we can really start designing and testing the circuit\. Some of the code should be writable before this point, though\.|

# Training Plan

- In order to complete this project, the team will have to be able to\.\.\.

- Program simple Arduino\-based code\.

- Ensure high transmission speeds over wires and ethernet cables\.

- 3D\-print uniquely designed parts with specified dimensions and holes\.

- Design a breadboard\-based circuit and PCB\.

- Measure signal transmission times\.

- Measure buzzer frequencies and volumes\.

- Perform general testing and troubleshooting\.

- Write a clear, professional instruction guide\.

- Learn how to program button debouncing\.

Table 5: Descriptions of Each Team Member’s Skillset

|Team member:|Relationship of Current knowledge and Skills To Project|
|---|---|
|Jacob|I have experience, both from past internships and past classes, with Arduino\-based programming, instruction guide creation, embedded circuit design, PCB and circuit construction, general project management, and a little bit of 3D\-printing\.|
|Bhargav|I have experience from past classes with designing circuits, embedded software\- development, PCB Design, and 3D Printing\.|
|Calvin|Lots of embedded systems design experience using Arduino from before and during Rose\. Experience in Arduino and C configuration\. Some experience with PCB design, more experience with UI development\.|
|Linus|Lots of general previous coursework experience, which includes embedded software development, embedded hardware design, testing, and PCB design\.|

Table 6: Descriptions of Training Each Team Member Will Undergo for the Project

|Assigned to:|Tasks performed and Artifacts Developed to learn new skills:|Due Date:|
|---|---|---|
|Jacob|Created \(now mostly obsolete\) research memo on other proposal for system\. Will go over example Arduino codes and will review PCB design principles and look into button debouncing code\.|10/31/24|
|Bhargav|Created a research memo on the different microcontrollers available\. Chose the Arduino Nano 33 IOT as the main microcontroller\. Researched different pushbuttons available and their different strengths and weaknesses\. Will research the different options for 3D printing and the different strengths and weaknesses of materials\.|10/29/24|
|Calvin|Researched the most cost\-efficient way to expand our pin allocation\. This involved how to perform inter\-device communication in Arduino, and how to interface with pin extenders\. Researched options for dividing a single power input, and using transistors to drive components with higher power requirements\.|10/29/24|
|Linus|Research cable options to connect the main control unit with the buzzer and timers\. Find options for a cheap, pin efficient 7 segment display\. Research options for a timing module\.|10/29/24|

# Resources and Budget

Purchase Plan:

Part orders for the project will be from one of two avenues\. Most parts will be ordered through the Rose\-Hulman ECE Department and the balance will be accumulated to be paid at the end of the project, Rose\-Hulman may be subsidizing a portion of this balance subject to the final decision of the ECE department\. Some parts will be directly ordered by the development team\. In these cases, receipts will be accumulated, and the balance and log of receipts will be sent to the clients at the end of each quarter\. Billing information will be provided upon sending the balance\.

Table 7: Descriptions of Resources Needed for Each Milestone, as well as Estimated Unit Cost and Development Budgets

Note: The Unit Cost Budget is the cost of the parts used in the final product\. The Development Budget is the cost of everything else, including other tested components\.

|Milestone|Description of Resources Needed|Estimated Unit Cost Budget|Estimated Development Budget|
|---|---|---|---|
|All parts decided on and ordered|2 3\-inch 7\-segment displays \(2\-digit numbers only, so need 2\)<br>2 1\-inch 7\-segment displays<br>7 25\-foot ethernet cables<br>14 ethernet cable ports<br>1 Arduino Nano 33 board<br>1 pin extender<br>6 button unit buttons<br>5 center console buttons \(Reset Timer \+ Boxes, Start Timer, Timer Up, Timer Down, and Next Answer/Reset Boxes\)<br>1 center console switch \(Switch Mode\)<br>6 LEDs<br>6 buzzers<br>12\+ resistors \(for buzzers and LEDS, values TBD\)<br>A good number of wires to connect parts together \(no point in setting a specific number\)<br>1 120VAC\-5VDC outlet\-micro USB cable|Proposed lump sum: $150 \(including cost of all electrical components, such as the ethernet cables and microcontroller boards\. Ideally, we won’t need to spend the whole amount\)\.|Proposed lump sum: $170<br>\(to cover experimentation or breakage with resistors, LEDs, microcontrollers, etc\.\)\.|
|Complete functionality using breadboard|Any parts we needed to replace/didn’t realize we needed|Proposed lump sum: $30 \(to cover last minute adjustments to meet verification requirements\)|$0|
|PCB produced and functional|Cost of milling|Proposed lump sum: $20 \(This is a very rough estimate of the on\-campus PCB mill, depending on complexity or size it could be higher or lower\)|Proposed lump sum: $20 \(for potential first draft of final design\)|
|3D\-printed parts created, durable, and functional|Costs of 3D\-printing, if there are any \(we’ll be using Rose\-Hulman’s 3D printed and engraving technology\)|$0|$0|
|Final completion|Posterboard for Rose Show|$0|Proposed lump sum: $10 \(for cost of printing\)|
||Total|$200|$300|

> （注：部分内容由豆包工作 AI 生成）
