# 项目完成报告（Final Report）

To

Dr Leanne Holder and Jim Reppell, MATHCOUNTS

Dr Jianjian Song, Rose\-Hulman Institute of Technology

From

Team 13

Calvin Hamilton

Jacob Rast

Linus Wise

Version 2\.0

May 20, 2025

MATHCOUNTS is a nationwide competition series for middle\-school students who are passionate about mathematics and problem\-solving\. In the final round of this competition, the Countdown Round, teams are tested against each other in a competition format using a buzzer system to determine who gets to answer when \[7\]\. The current system is outdated, using a computer from the 1990s with a configuration that is difficult for a new operator to figure out\. The hardware components have also long since been discontinued, making the acquisition of replacement parts nearly impossible\. Commercially available systems do exist but are only capable of basic operations and do not offer the answer queuing format used in MATHCOUNTS, nor the option to switch game modes depending on the user\. These systems are also expensive for what they are, so the goal of this project was to create a cheaper and more flexible alternative that would replace the existing system and expand on its functionality \[12\]\.

The new system consists of three main components\. The first is a new “center console” that has five easy\-to\-understand buttons for simple commands, as well as two switches: one to turn power on and off and another to switch between operational modes\. The center console contains a small, embedded timer display, which shows how much time is left in a round—a metric also displayed on the second main component, the external “timer unit,” which is outward\-facing and adjustable so contestants can view it\. The third component is the “button box,” which students use to buzz in to indicate they wish to answer\. Between two and six of these units can be plugged in at a time, with system functionality unchanged regardless of the number of units connected\. Both the button boxes and the timer unit are connected to the center console via Ethernet cables, which can be easily replaced\. In addition to manual control, more complex game operations can be managed via Bluetooth, which can also allow remote control of the system\. The new system has not only met all the features desired by the MATHCOUNTS competition organizers—it has generally exceeded them, making this prototype a great success\.

1\. Introduction and Project Overview	4

1\.1\. Overview	7

1\.2\. Stakeholders	8

1\.3\. Project Deliverables	9

1\.4\. Features	11

1\.5\. Specifications	12

1\.6\. Conclusions	18

2\. Accomplishments	19

2\.1\. User Guide	19

2\.2\. Design and Implementation of the Center Console	20

2\.2\.1\. Circuit Design of the Center Console	20

2\.2\.2\. PCB Layouts for the Center Console	22

2\.2\.3\. Center Console Enclosure	25

2\.2\.4\. Selection of Cables	26

2\.2\.5\. Software Implementation	26

2\.3\. Design and Implementation of the Button Boxes	28

2\.3\.1\. PCB Layouts for the Button Boxes	29

2\.3\.2\. Button Box Enclosure	30

2\.4\. Design and Implementation of the Timer Unit	32

2\.4\.1\. PCB Layouts for the Timer Unit	32

2\.4\.2\. Timer Unit Enclosure	33

2\.5\. Design of the Bluetooth GUI	35

2\.6\. Verification of Requirements	36

2\.6\.1\. Verification for Unit Cost \(Requirement A\-1\)	41

2\.6\.2\. Verification for Type 1 Mode Accuracy \(Requirement F\-6\)	42

2\.6\.3\. Verification for Type 2 Mode Accuracy \(Requirement F\-5\)	43

2\.6\.4\. Verification for Response Time \(Requirement B\-5\)	45

3\. Project Plan and Timeline	48

3\.1\. Planned Timeline	48

3\.2\. Implemented Timeline	50

4\. Budget	52

5\. Recommendations	54

6\. Going Further Material	56

6\.1\. I2C and RJ45 Standards	56

6\.2\. Impacts of the Project on Broader Issues	56

6\.2\.1\. Public Health, Welfare, and Safety	57

6\.2\.2\. Global Issues	57

6\.2\.3\. Societal Issues	58

6\.2\.4\. Economic Issues	58

6\.2\.5\. Environmental Issues	59

6\.3\. System Failure Mode Analysis	59

7\. Annotated Bibliography	61

8\. Appendices	64

8\.1\. Appendix A: User Guide	64

8\.2\. Appendix B: Bluetooth Commands, Explained	66

Please indicate a single page number for an example of each of the following types of figures that explains an important part of your project\. If that type of diagram does not exist in your project, please put N/A for the page number\.

Circuit Schematic \(either analog or digital such as microcontroller\): 21

User interface or other user experience design: 35

Flow chart/UML diagram/or other similar diagram of code function or structure: 27

Logic diagram such as FPGA architecture: N/A

Mechanical diagram such as CAD drawing: 26

Other type of diagram such as CST diagram, PCB design, etc\.: 24

# Introduction and Project Overview

In the MATHCOUNTS countdown round, two to six competitors are presented with a question and given ten to ninety\-five seconds to answer\. Every competitor is provided with a button unit that is wired to a center console which resides in front of the competition operator\. After the first competitor to buzz in responds to the question, and depending on the rules chosen by the host, either \(in Type 2 Mode\) the second student to buzz in is allowed to answer, with contestant’s order of answering being stored in a queue, or \(in Type 1 Mode\) the buzz\-in is cleared, allowing all of the other students get another chance to buzz in\. Regardless, only one unit at a time will light up and trigger a sound from the center console, be that the first to have its button pressed in a round or, if in Type 2 Mode, when the operator moves the next contestant in the queue\. In the current system, this is facilitated with six LED units, six handheld pushbuttons, a computer, and a unit to interface all the buttons and LEDs to said computer\. This is no longer an ideal setup, however—not only are the current software and computer outdated and not familiar to most users, but the hardware itself also uses outdated ribbon cabling and connectors which are hard to acquire in the event of failure\.

Our clients desired that our new system be easier to use for the operator while still maintaining the critical functionality of allowing the operator to go to the next competitor who buzzed in, reset the button units, switch between operating modes, and manage the timer\. Furthermore, in addition to the small, embedded display facing the operator, the new system was also to have an additional timer unit that would allow the competitors to clearly see the remaining time\. Both timers were to display the same time and be controllable by the competition operator via the center console, with them being able to start the timers, reset the timers, or change the starting time at which the countdown would begin\. Figure 1, below, shows the setup of our final product, showcasing how the competitors with their button boxes are able to view the time remaining, while Figure 2, below, displays the difference between the preexisting system and our replacement\.

MATHCOUNTS competition setup

Old setup \(left\) vs new setup \(right\)

As mentioned, the center console is the most important part of the system and serves as both a receiver and transmitter of all commands made by the operator throughout the course of the competition\. The unit has buttons and switches embedded, with prominent placement and labels making it clear which input does what\. Furthermore, the center console houses the system’s speaker, from which the sounds of the competition will be played throughout the round\. As is shown in Figure 3, below, the switches are located at the top of the unit, power on the left and mode on the right, with the five buttons near the bottom being, in order, Start Timer, Reset Timer, Next To Answer, Timer Up, and Timer Down\. Furthermore, near the top of the unit is the aforementioned built\-in timer display, allowing the operator to view the time remaining without needing to look at the external timer unit\. Finally, there is a plug for power located on the right side of the console, as well as seven RJ45 Ethernet ports along the back to connect to, in order, the external timer unit and the six connectable button boxes\.

The center console

The timer unit, seen below in Figure 4, is separate from the center console and prominently displays time on two seven\-segment displays, serving to reflect a two\-digit time value\. An RJ45 Ethernet port is located at the back of the timer and handles communication and power when connected to the labelled port on the center console\.

The timer unit

The button boxes, one of which is seen below in Figure 5, are used by the contestants to input answers as well as to tell the contestants when they are allowed to answer via the built in LED\. When a rival contestant’s LED is lit up, all the other players, as well as the audience, will know that it is that student’s time to answer, although if the system is in Type 2 Mode, the other students will still be able to buzz in to add themselves to the queue when this is the case\. Up to six of these devices can be connected via the RJ45 Ethernet port on the back of each individual box to six on the back of the center console\.

A button box

## Overview

Our new system uses an Arduino Nano 33 IOT microcontroller \[13\], which interfaces with six button units, two timer displays, and a set of button and switch inputs on the center console\. As mentioned, Ethernet cables are used to connect all external devices to the center console components with embedded RJ45 Ethernet connector ports\. The device, which takes 9V as an operating voltage, can be powered with any wall outlet\. Furthermore, as the system is expected to last several years, we designed it to be more durable by installing custom PCBs on both the lids and bases of the 3D printed enclosures, Ethernet ports for easy connection and disconnection of devices \(in case a cable needs to be replaced\), and rubber padding on the bottoms and sides of the units for added protection\. Furthermore, a built\-in Bluetooth chip and included GUI program allow the operator to control the system from anywhere in a 25\-foot radius, not just by way of the center console\.

The center console and GUI feature 5 main controls to control the system’s behavior\. “Start Timer” and “Reset Timer” are straightforward, with the former beginning the countdown of the timer values and enabling contestant answering and the latter resetting the time to the preset initial time value and disabling buzz\-ins\. Furthermore, as mentioned, in order to align with client specifications and support different competition formats, two system modes are included within this device, both of which center around the functionality of the “Next To Answer” button and which can be chosen between using the built\-in switch on the center console\.

In Type 1 Mode, when a competitor is the first to press their button in a round and answers incorrectly, pressing “Next To Answer” will reset all buzzers, turning off that competitor’s LED and again allowing any competitor to buzz \(as long as there is time left in the round\. In Type 2 Mode, the system remembers the order of all the buttons pressed in a round and stores them in a queue, such that when a competitor answers incorrectly, the operator can press the “Next To Answer” button to go to the next competitor who has buzzed in\. If that competitor’s answer is also incorrect, the operator can then move to the third to buzz in and so on, going down the queue until either a correct answer is given, at which point the round will end, or until all contestants to have buzzed in have given an incorrect answer, at which point “Next To Answer” functions as it does in Type 1 Mode, giving all competitors another chance to get it right\.

In both modes, the timer will not stop either when a competitor buzzes in or when the next contestant in queue is moved to\. Additionally, by using the “Timer Up” and “Timer Down” buttons, the length of the timespan of the timer can be changed by five second increments, from 10 seconds to 95 seconds\. Additional commands and tuning can be achieved with specific Bluetooth commands detailed in the user guide, located in Appendix A\.

## Stakeholders

In order to ensure that we were designing the new buzzer system such that it would fully satisfy all who would be affected by it, we wrote down a list of all critical stakeholders, as well as the ways in which the features of our system would affect them \[7\]\. The end result of this is shown below in Table 1\.

Stakeholders and their associated features

|Stakeholder Name|Associated Features|Description of the stakeholder with respect to the associated features|
|---|---|---|
|Organizers|Affordable<br>Durable<br>Transportable|The organizers are the people who organize and run the competition\. They want the system to be affordable, since the budget is limited\. The system will be used for at least a decade, so it must be durable enough to last throughout that time\. Additionally, the system must be able easily to be transportable and survive normal use\.|
|Operators|Intuitive<br>Controller<br>Timer Unit|Operators are the people on the controller controlling the entire system\. They want the system to be durable and easy to use\. They prefer that the buttons on the controller be clearly labelled and easy to read\.|
|Competitors|Intuitive<br>Button Boxes<br>Timer Unit|The competitors are the stakeholders competing in MATHCOUNTS\. They will use a button box to buzz in and can see a timer unit that displays how much time is left to buzz in\.|
|Audience|Intuitive<br>Button Boxes<br>Timer Unit|The audience are the people who are watching the competition\. They want to focus on the competition, such as who buzzed in to answer the question\. The system must be accurate and responsive to run the competition correctly\.|

## Project Deliverables

To ensure we met the specifications laid out for us by our clients, as well as that our replacement system would facilitate the MATHCOUNTS competition itself, we wrote out a list of deliverables we needed to have produced by the end of the year, which included \[7\]:

- A functional power supply for the controller using a wall outlet

- A Bluetooth app to mirror center console commands

- Six pushbutton boxes that are connected using Ethernet to the controller

- A LED/light on each pushbutton box that lights up when a competitor presses the button

- A seven\-segment display that shows the competitors the remaining time on a separate unit

- A main controller that interfaces between the pushbuttons and the operator\(s\), which includes:

- “Timer Up” and “Timer Down” buttons for setting the start time of the timer to 	anywhere between 10 and 95 seconds in 5\-second increments

- A "Reset Timer \+ Boxes” button for resetting the timer and the six pushbutton boxes

- A “Start Timer” button for starting the time

- A “Reset Boxes/Next Answer button that resets the pushbutton boxes and either does or does not go to the next person to answer based on mode

- A "Change Mode” switch to switch between two different modes of the system

- A buzzer that plays when time runs out or a competitor pushes their button \(three sounds total—one for timeout, two for buzz\-in\)

- A seven\-segment display on the operator’s controller to show them the remaining time

Happily, we were indeed able to meet all of these deliverables, the results of which are shown below in Figures 6 and 7\.

Labelled system overview

All units viewed from back

## Features

To make sure our time was well spent throughout the production of this system, we were inclined to lay out a set of features the final product needed to have near the outset of our production process, in order to give us a better idea of what we should be working on and how it could be used to satisfy our client’s desires of us\. The final product of this process is displayed in Table 2, below\.

High level feature definitions

|Feature|High\-Level Definition|
|---|---|
|Affordable|The system must not be too costly to develop or construct\.|
|Durable|The system should be able to withstand the wear and tear of a middle school competition\.|
|Intuitive|The system should be easy for an operator to understand with the aid of an instructional guide\.|
|Transportable|The system must still function after being transported by car and must not be overly cumbersome\.|
|Button Boxes|The button boxes must light up and sound when first to be pressed in a round, but not when they are second or later\. These later button presses must still be recordable, though\.|
|Center Console|The center console must be able to control the timer units and button boxes according to the competition standards\. It must also be able to pair with a smartphone via BLE through usage of a “pair” button\.|
|Timer Unit|The four timer units \(two sets of two digits\) must be adjustable and must show the contestants and operator the remaining time for a question\.|
|Ethernet Cables|The cables must carry signals between the center console and button boxes, as well as the center console and the contestant\-facing timer units\.|
|GUI Option|All button/switch controls of the device \(Start Timer, Reset Timer, Timer Length Up, Timer Length Down, Mode Switch, Next Answer\) can be controlled via a python\-based executable file which has a graphical user interface that can perform all commands of the central controller\.|

## Specifications

In order to properly demonstrate to our clients that our system met the requirements they had of it, as well as to give us some means of testing the functionality and quality of our implementation once the initial prototype had been completed, we assembled a set of requirements we wanted the system to meet, as well as the validation for including them, the way in which we would verify whether or not we had met them, and details of the testing that would be required to do so\. The result of this planning is displayed in Table 3, below\.

Specific requirements and verification methods

|Features|ID|Requirement|Validation|Verification|Design for Test|
|---|---|---|---|---|---|
|Ethernet Cables, Transportable|E\-1|Ethernet cables will be at least 25 feet long\.|Must be long enough to allow room between contestants and operator\.|We will record the length from start to end using a tape measurer|We will only measure the length of one cable \(having ordered in bulk, they will all be the same length\)\.|
|Ethernet Cables, Transportable|E\-2|Ethernet cables will be coil\-able\.|Must be able to transport cable easily and without breakage\.|We will coil one cable around our arm, verify that it stays in place, then uncoil and plug back in, to verify that it still works\.|Again, we will only need to test this with one cable\. In more detail, we will coil the cable by holding one end and wrapping the cable around our elbow and palm\.|
|Ethernet Cables|E\-3|Ethernet cable connectors will adhere to the T568B standard for RJ45 ports\.|Must be standardized to work with Ethernet Cables\.|We will make sure the RJ45 ports we purchase adhere to the standard\.|We will select parts that are shown to follow the T568B standard|
|Affordable|A\-1|Unit cost will be below $200\.|Must not be too expensive to replicate in case of wider adoption\.|We will keep a BOM table and keep track of the costs of parts in the final design\.|Only parts included in full, finalized model with be counted for this cost—not replacement parts or prototyped but ultimately discarded components\.|
|Affordable|A\-2|Development cost will be below $300\.|Our clients would prefer that the development does not take too many resources\.|We will keep a BOM table and a list of receipts in order to actively track this\.|All parts purchased but not used in the final design will be counted for this cost\. The total budget is the sum of the unit and development costs: $500\.|
|Durable|D\-1|The system will be able to withstand 100 button presses with at least a 0\.5 second gap between them\.|This is needed to properly operate over the span of a competition\.|We will test this by rapidly pressing each button 100 times, making sure that the gap between presses is above 0\.5 seconds\.|This will be tested for the buttons on the button boxes—the center console buttons should not need to handle this much force\. Additionally, to be clear, the testing will occur after all 100 button pressed have been executed; the point of this requirement is to test the buttons’ long\-term viability, not to verify the accuracy of button inputs \(as that is tested in requirement F\-1\)\.|
|Durable|D\-2|The system will still be fully functional after a drop of three inches\.|If the system is dropped off a table, it should not break\.|We will individually drop the button boxes and center console, then test basic operations to confirm functionality\.|We don’t wish to damage the system by being overly forceful, so we will only test this once per unit\. The height will be measured using a ruler or tape measurer\.|
|Durable|D\-3|The system will still be fully functional after being shaken for one minute|The system should be transportable by car\. This is designed to simulate driving over bumpy roads\.|We will individually shake the button boxes and center console for one minute, timed with a stopwatch|Again, we will only test this once, as long as it passes, to avoid overly taxing the system\.|
|Durable, Ethernet Cables|D\-4|The Ethernet cables are stepped on by someone at least 150lbs\.|This is a reasonable situation for the system to be in, so we will make sure the system will still work if it occurs\.|Someone who knows their weight to be above 150lbs will briefly step on a cable to a button box and the cable to the timer unit\. We will then test basic functionality\.|As Ethernet cables are designed to be sturdy, we will be able to test this multiple times\. 150lbs is just chosen as a reasonable weight for the average adult\.|
|Transportable, Center Console|T\-1|The center console will be smaller than 12’’ by 14’’ by 5’’\.|We do not want the system to be too cumbersome to carry and set up\.|We will measure all sides using a ruler\.|The measurement will be done of the completed casing, not just the parts on a breadboard\.|
|Transportable, Center Console|T\-2|The center console will be lighter than 15 pounds\.|We do not want the system to be too cumbersome to carry and set up\.|We will weigh the center console, disconnected from any cables, using a scale\.|The measurement will be done of the completed casing, not just the parts loose on a scale\.|
|Transportable, Button Boxes|T\-3|The button boxes will be smaller than 2’’ by 3’’ by 1\.5’’\.|We do not want the system to be too cumbersome to carry and set up\.|We will measure all sides using a ruler\.|The measurement will be done of the completed casing, not just the parts on a breadboard\.|
|Transportable, Button Boxes|T\-4|The button boxes will be lighter than 2 pounds each\.|We do not want the system to be too cumbersome to carry and set up\.|We will each individual box, disconnected from any cables, using a scale\.|The measurement will be done of the completed casing, not just the parts loose on a scale\.|
|Functional, Button Boxes|F\-1|The correct button box will light up and sound off 100 times out of 100\.|This is a basic requirement for a competition\. Accuracy is key\.|We will test this by hand, making sure there are six contestants and at least 100 buzz\-ins\.|We will use a simple counter to track buzz\-ins\. We will also be sure that all contestants buzz in first at some point, and that all other contestants attempt to buzz in when someone else’s light is already on for each contestant\.|
|Functional, Center Console, Timer Unit, Button Boxes|F\-2|The Reset Timer button will reset and stop the timers and clear all lights 30 times out of 30, regardless of what else is happening in the system’s operation\.|This is a basic requirement for a competition\. The ability to properly reset is key\.|We will test this by hand, making sure there are six contestants and at least 30 total resets\.|We will test a reset with each contestant’s box lit up 2 times each, once in each mode\. Additionally, we will test that the timers reset to the time set by the initial timer length setup for all initial timer lengths, as well as while running and when at 0\.|
|Functional, Center Console, Timer Unit, Button Boxes|F\-3|The Start Timer button will start the timers, counting down from whatever the initial timer length value is, and allow for buzz\-ins 30 times out of 30\.|This requirement is common sense\. If the timers aren’t starting, the competition can’t go on\.|We will test this by hand, testing each of the 20 possible starting points for the timer, as well as checking that buzz\-ins work as expected\.|We will test that all 6 button boxes don’t light up when a button is pressed before Start Timer is pressed but do light up afterwards\. This will be tested with each button box being the first pressed for an iteration, in order to make sure the failure to light up is not due to incorrectly thinking another button box is lit\.|
|Functional, Center Console|F\-4|12 times out of 12, the Change Mode switch will be checked whenever the start timer button is pressed, and for the round, behaviors will act accordingly, as long as the Change Mode switch was changed at least 100ms before the start button was pressed\.|If the rise/fall of the signal happens at the same time that the Start Timer button was pressed, it will not be possible to accurately predict which mode should be picked, so we have put in place this limitation\.|We will test this by hand, switching the mode at various times during a round to ensure that behavior is not updated until the start timer button is pressed \(making sure not to press it within 100ms of changing the switch\) and 6 times for each mode to ensure the change occurs\.|We will test that Type 1 mode is now enabled by ensuring that all LEDs turn off and all button boxes are re\-enabled regardless of if another contestant buzzed in while the first contestant’s LED was still on after the Next Answer button is pressed\. We will ensure that Type 2 mode is now enabled by ensuring that instead the first contestant’s LED turns off and the second contestant’s turns on, not allowing other contestants to buzz in, when the Next Answer button is pressed\.|
|Functional, Center Console, Button Boxes|F\-5|30 out of 30 times, the Next Answer button will, while in Type 2 mode, turn off the current contestant’s LED and turn on that of the next contestant, while playing the secondary buzz\-in sound\. If no more contestants had buzzed in, this button will act as it does in the Type 1 mode and turn off all LEDs while re\-enabling buzz\-ins\.|This is needed to ensure the functionality of the Type 2 mode\.|We will test this by hand, running 30 experiments after having pressed the Change Mode button, as described above\.|We will test each buzzer unit being the first to buzz in, each being the second to buzz in, and each being the only to buzz in for a particular question\. After that, we will run 12 more iterations spanning different arrangements and numbers of buzz\-ins, making sure to test 1\-5 contestants buzzing in after the first contestant and iterating all the way through the loop\.|
|Functional, Center Console, Button Boxes|F\-6|21 out of 21 times, the Next Answer button will turn all LEDs off and allow for new buzz\-ins when in Type 1 mode\.|This is needed to ensure the functionality of the Type 1 mode\.|We will test this by hand, making sure to have pressed the Change Mode button an even number of times\.|We will test that this button clears all LEDs while still allowing for continued play if no contestants had buzzed in \(at which point nothing will change\) and with each contestant having been the one to buzz in first\. This will be 7 trials total, so we will repeat it three times for certainty\.|
|Functional, Center Console, Timer Unit|F\-7|20 out of 20 times, the Increase Timer Length button will increase the timer’s start point by 5 seconds \(unless already at 60s\)\.|The client specified that the timer should be adjustable between 20 and 60 seconds\.|We will test this by hand, increasing by increments of five starting from 20\.|We will test this concurrently with the Decrease Timer Length button\. We will also make sure that the timer length is reflected when the timer is started, and that it doesn’t go beyond 60 by pressing it when it’s already there\.|
|Functional, Center Console, Timer Unit|F\-8|20 out of 20 times, the Decrease Timer Length button will decrease the timer’s start point by 5 seconds \(unless already at 20s\)\.|The client specified that the timer should be adjustable between 20 and 60 seconds\.|We will test this by hand, decreasing by increments of five starting from 20\.|We will test this concurrently with the Increasing Timer Length button\. We will also make sure that the timer length is reflected when the timer is started, and that it doesn’t go beyond 20 by pressing it when it’s already there\.|
|Functional, Center Console, Timer Unit|F\-9|Both Increase Timer Length and Decrease Timer Length will only work when system is at the initial \(reset\) state\.|This exists to avoid confusion if the operator accidentally presses the button during the countdown\.|We will test each button during multiple other stages of operation\.|We will test this for the three modes of the system’s operation: when the timer is stopped at 0, when the timer is stopped at the initial time, and when the timer is ticking down\. This button should only change the timer length when in the second of these states\.|
|Functional, Center Console, Button Boxes, Timer Unit|F\-10|Button boxes will not light up after the timer has reached 0\.|This exists to make clear that the time for the question has ended\.|We will test this by pressing all buttons on button boxes, as well as the Next Answer button\.|The Next Answer button will be tested after making sure there are contestants in the queue\.|
|Timer Unit|U\-1|After starting, both timer units will tick down at a rate accurate to actual time within 20ms\.|Accuracy of timekeeping is important in any time\-based competition\.|We will test this with a stopwatch\. By starting both at the same time, we should be able to determine the degree of drift|We will write a separate test code that flips one segment of one of the seven segment displays every second based on the timer the microcontroller is operating with\. We will then hook up the output of this segment to an oscilloscope and measure the time in which the signal is high each cycle\. If this is within 20ms of one second, this requirement will be verified\.|
|Timer Unit, Center Console|U\-2|Signals coming from the center console and going to the timer unit other than just ticking down will be reflected within 20ms\.|Quickness of these signals will make the job of the center console operator smoother\.|We will test this by viewing the signals coming into the timer unit and out of the center console when a center console button is pressed\.|More specifically, we will use an oscilloscope to measure the gap between rise/fall times for all center console operations that directly affect the time displayed on the timer units: Start Timer, Reset Timer, Increase Timer Length, and Decrease Timer Length\. We will test these for both timer unit pairs\.|
|Button Boxes|B\-1|Inputs from the button boxes will be reflected in the center console within 10ms of button press\.|Considering how quickly contestants will hammer down on the buttons, high resolution is important here\.|We will test this by viewing the signals coming into the center console and out of the button box when a button is pressed\.|More specifically, we will be using an oscilloscope to measure the gap between rise/fall times\. When a button box button is pressed, it is first sent to the center console to verify that the LED can be lit up—we will measure the time between pressing the button and this signal arriving to the center console for all six button boxes\.|
|Button Boxes, Center Console|B\-2|Signals originating from center console and going to button box will register in under 20ms\.|The signals from the center console are not as urgent, so don’t need as high a resolution, but still need to be quick enough to facilitate the competition\.|We will test this by viewing the signals coming out of the center console and into the button boxes\.|We will use an oscilloscope hooked up to both the center console and the button box\. This requirement only includes signals originating from the center console that affect the button boxes: Next Answer \(currently\-on LED should turn off and next in queue should turn on if in Type 2 Mode\) and Reset Timer \(currently\-on LED should turn off\)\. We will measure the difference between rise/fall times for all six boxes\.|
|Button Boxes, Center Console|B\-3|Signals originating from button boxes but going through center console will take under 20ms to travel back from center console to button box\.|This is the key to the competition—making sure button presses light up the LEDs and sound off the buzzers—so good resolution is important\.|We will test this by viewing the signals coming out of the center console and into the button boxes\.|We will use an oscilloscope hooked up to both the center console and the button box\. After the button box button is pressed, the signal travels to the center console, which then, if that press was first for the question, sends back a signal to turn on the LED and buzzer\. We will measure the difference between rise/fall times of that sent\-back signal for all six boxes\.|
|Center Console|C\-1|Two audibly different \(at least 100hz apart if one note or three different sequences of notes\) noises will come out of the buzzer in the center console depending on whether the contestant was the first to answer the question or the second to sixth\.|This is a request from our client and will make “next answer” moments a little more exciting\.|We will test the frequencies of the buzzer in both scenarios to make sure they are at least 100hz apart, such as to be audibly different\.|More specifically, we will hook up an oscilloscope to the buzzer, then get buzzes from both pressing the button box’s button and the Next Answer button \(after making sure that button is next in the queue\)\. We will test this for all six boxes\.|
|Center Console|C\-2|All noises produced by the buzzer will be at least 50 decibels at a range of 25 feet\.|This is a reasonable range to the point that the noises are audible, but not painful, for both the operator at the center console and the contestants 25ft away\.|We will use the app Decibel Pro for the iPhone in order to determine this\.|We will test this for all three possible noise sequences coming out of the buzzer both while at the center console and while at the button boxes\.|
|Center Console|C\-3|All noises produced by the buzzer will be between 220 and 1480hz\.|This is a reasonable range for the noises to be audible for all involved\.|We will use the tuning website OnlineMicTest\.com in order to determine if the frequencies match this requirement\.|We will play the noises in an otherwise quiet room and make sure that none of the notes are outside of this prescribed range\.|
|Center Console, Gui Option|C\-4|A Bluetooth connection can be made to the central controller, so long as it has been powered for longer than 30 seconds, and the connecting device is within 30 feet of the controller center\.|Any more than this amount of time would be unreasonable to ask the operator\. It should maintain its connection for a reasonable range\.|After turning on the controller, we will wait 30 seconds and then attempt to connect to Bluetooth\. It should stay connected until we move more than 30 feet away|We will measure the exact time using a stopwatch\. We will measure the disconnection distance with a tape measure\.|
|Gui Option|P\-1|The BLE connection between the smartphone and the buzzer system must last for two hours, including after the smartphone goes into sleep mode\.|This is a high estimate for how long the competition will last\.|After pairing the devices \(indicated by the top banner on the GUI, as seen in Figure 13, below\), we will wait this long and see if they are still connected\.|We will measure the exact time using a stopwatch and wait for the smartphone to go into sleep mode automatically\.|
|Functional, Gui Option, Timer Unit, Button Boxes|P\-2|The Reset Timer button, pressed on the GUI, will reset and stop the timers and clear all lights 6 times out of 6, regardless of what else is happening in the system’s operation\.|This is a basic requirement for a competition\. The ability to properly reset is key\.|We will have already tested this with buttons, so no need to test so many times\.|We will test a reset with each contestant’s box lit up once each\. Additionally, we will test that the timers reset to the time set by the initial timer length setup for all initial timer lengths, as well as while running and when at 0\.|
|Functional, Gui Option, Timer Unit, Button Boxes|P\-3|The Start Timer button, pressed on the GUI, will start the timers, counting down from whatever the initial timer length value is, and allow for buzz\-ins 5 times out of 5\.|This requirement is common sense\. If the timers aren’t starting, the competition can’t go on\.|We will test this by hand, five different starting points for the timer, as well as checking that buzz\-ins work as expected\. No need to test so many times again\.|We will test that all 6 button boxes don’t light up when a button is pressed before Start Timer is pressed but do light up afterwards\. This will be tested with each button box being the first pressed for an iteration, in order to make sure the failure to light up is not due to incorrectly thinking another button box is lit\.|
|Functional, Gui Option|P\-4|2 times out of 2, the Change Mode switch, toggled in the GUI, will be checked whenever the start timer button is pressed, and for the round, behaviors will act accordingly, as long as the Change Mode switch was changed at least 100ms before the start button was pressed\.|If the rise/fall of the signal happens at the same time that the Start Timer button was pressed, it will not be possible to accurately predict which mode should be picked, so we have put in place this limitation\.|We will test this by hand, switching the mode at various times during a round to ensure that behavior is not updated until the start timer button is pressed \(making sure not to press it within 100ms of changing the switch\) and 2 times for each mode to ensure the change occurs\.|We will test that Type 1 mode is now enabled by ensuring that all LEDs turn off and all button boxes are re\-enabled regardless of if another contestant buzzed in while the first contestant’s LED was still on after the Next Answer button is pressed\. We will ensure that Type 2 mode is now enabled by ensuring that instead the first contestant’s LED turns off and the second contestant’s turns on, not allowing other contestants to buzz in, when the Next Answer button is pressed\.|
|Functional, Gui Option, Button Boxes|P\-5|5 out of 5 times, the Next Answer button, when pressed in the GUI, will, while in Type 2 mode, turn off the current contestant’s LED and turn on that of the next contestant, while playing the secondary buzz\-in sound\. If no more contestants had buzzed in, this button will act as it does in the Type 1 mode and turn off all LEDs while re\-enabling buzz\-ins\.|This is needed to ensure the functionality of the Type 2 mode\.|We will test this by hand, running 5 experiments after having pressed the Change Mode button, as described above\. No need to test as many times as before\.|We will test the switch from one LED to another twice, the turning off of all LEDs twice, and the waiting for a new contestant to buzz in once\.|
|Functional, Gui Option, Button Boxes|P\-6|7 out of 7 times, the Next Answer button, when pressed in the GUI, will turn all LEDs off and allow for new buzz\-ins when in Type 1 mode\.|This is needed to ensure the functionality of the Type 1 mode\.|We will test this by hand, making sure to have pressed the Change Mode button an even number of times\. No need to test as many times as above\.|We will test that this button clears all LEDs while still allowing for continued play if no contestants had buzzed in \(at which point nothing will change\) and with each contestant having been the one to buzz in first\. This will be 7 trials total, so we will repeat it once each\.|
|Functional, Gui Option, Timer Unit|P\-7|5 out of 5 times, the Increase Timer Length button will increase the timer’s start point by 5 seconds \(unless already at 60s\)\.|The client specified that the timer should be adjustable between 20 and 60 seconds\.|We will test this by hand, increasing by increments of five starting from 45\. No need to test as many times as above\.|We will test this concurrently with the Decrease Timer Length button\. We will also make sure that the timer length is reflected when the timer is started, and that it doesn’t go beyond 60 by pressing it when it’s already there\.|
|Functional, Gui Option, Timer Unit|P\-8|5 out of 5 times, the Decrease Timer Length button will decrease the timer’s start point by 5 seconds \(unless already at 20s\)\.|The client specified that the timer should be adjustable between 20 and 60 seconds\.|We will test this by hand, decreasing by increments of five starting from 35\. No need to test as many times as above\.|We will test this concurrently with the Increasing Timer Length button\. We will also make sure that the timer length is reflected when the timer is started, and that it doesn’t go beyond 20 by pressing it when it’s already there\.|
|Button Boxes, Center Console, Gui Option|P\-9|Signals originating from center console, but selected via the GUI, and going to the button boxes will register in under 1\.02s\.|The signals from the center console are not as urgent, so don’t need as high a resolution, but still need to be quick enough to facilitate the competition\. More latency is added for the bluetooth connection, though\.|We will test this by viewing the signals coming out of the center console and into the button boxes\.|We will use an oscilloscope hooked up to both the center console and the button box\. This requirement only includes signals originating from the center console that affect the button boxes: Next Answer \(currently\-on LED should turn off and next in queue should turn on if in Type 2 Mode\) and Reset Timer \(currently\-on LED should turn off\)\. We will measure the difference between rise/fall times for all six boxes\. As we can’t hook up the oscilloscope to the smartphone, we’ll have to make a rougher estimate of the phone\-\>center console timing using a stopwatch\.|

## Conclusions

Upon analysis, all the units work well when connected and are reliable in their usage as described in the previous section\. System behavior matches what is expected, with the rate at which the timer counts down and the difference in time between signal inputs and changes being reflected in the system having been given particular attention and having passed our tests with no issues, as will be discussed in the following section\. Furthermore, the Bluetooth system easily connects to a laptop with our GUI executable installed on it, allowing the same functionality as the center console even while the operator is not physically at the console\. The Ethernet cables also allow the contestant’s buttons to be used from at least twenty\-five feet away, just as they will be in the countdown round in person\. Indeed, only a small number of requirements were not met, as will be discussed in Section 2\.6, below\. In that section, we discuss the requirements we laid out for this system, the rationale behind them, the ways in which we tested them, the results of those tests, and what those results mean for that requirement, as well as a more specific analysis of several of the requirements we did meet, such as one concerning the speed of the signal coming out of the center console, and the few that we did not, such as the mode switch option on the GUI\.

# Accomplishments

In order to complete a new, updated, and modular system to run future MATHCOUNTS competitions, we endeavored to construct a full system, designed to meet our clients’ initial specifications, as is seen in Figures 6 and 7, part of Section 1\.3\. In simple terms, we were tasked with constructing three separate components: a center console, through which an operator could control the flow of the competition with buttons and switches and a small timer unit; six button units, wired to the center console, through which two to six competitors could participate in the competition by “buzzing in” \(pressing a button and having an LED light up to indicate who pressed their button first\); and an external timer unit which would face the competitors, showing them how much time they had left in a round\. Later in the production of the system, we were even able to implement a Bluetooth option, such that the system could be operated using a Python\-based GUI on a laptop in addition to the already existing features\.

In terms of broader requirements, we wanted the system to be affordable, so that it could be replicate across different MATHCOUNTS chapters at a reasonable cost; functional, so that there would be no issues during the flow of the competition; durable, so that if the system was handled a little roughly, it would still work \(a reasonable concern in a competition for middle schoolers\); and transportable, so that it could be easily moved between competition centers\.

We are proud to say that we met all of these requirements with flying colors\. The total unit cost, not including shipping, ended up being approximately $97; we ran into no issues in the core operations of the system other than an issue with the bus capacitance with the I2C communication, which will be discussed in a later section; all drop tests had no effect on the functionality of the system; and it was all light enough and compact enough that it could be carried in a single tote bag\. While not every requirement we set out for ourselves was met in the end, we are still very satisfied with how the system turned out\.

## User Guide

In order to guarantee ease of use on the part of the operator, an operation guide is included with the system, which covers all aspects of the setup and running of the system, including Bluetooth commands\. This guide can be found in Appendix A\. There may be some repeated information between this and other sections, but we still felt that providing this guide in full would give a useful view into how the system will actually be utilized\.

## Design and Implementation of the Center Console

The center console, displayed again in Figure 8, below, is the main part of the system, acting as the interface for both the button box units and the timer unit\. The top of the console features labelled buttons for the five key commands which the operator can use to manage the flow of the competition\. Furthermore, the two\-digit seven segment display in the center serves to indicate the remaining time in a round, and two switches \(also labelled\) are also included, to be used for power and determining the game mode\. The speaker is located on the left side of the unit and the power jack on the right\. The back of the unit features seven labelled RJ45 Ethernet ports, used to connect the center console to up to six button boxes and the timer unit\. The main microcontroller within this center controller is an Arduino Nano 33 IOT, which has built\-in support for Bluetooth, UART and I2C, allowing for easy implementation of both the Bluetooth GUI feature and pin extenders, which serve to handle the high pin count requirements for the seven\-segment displays\.

The center console

### Circuit Design of the Center Console

The center console has many features and components, which required us to design a great deal of circuitry in order to work out how they would all connect\. Figure 9, below, shows the circuit schematic for the center console, with Figure 10 showing that of the embedded timer unit\. Most of this is standard embedded systems circuitry\. For example, the seven\-segment displays use a standard LED circuit, wherein each LED is in series with a resistor, selected to allow a specific amount of current to flow through said LED\. In the large seven\-segment display, the voltage source was 9V, the diode drop was 6\.8 V, and we needed 10 mA of current, so we picked 220 ohm resistors to provide said current\.

The speaker, however, required more complex circuitry\. We needed to provide 1A of current through the speaker, but the standard 3\.3 V rail on the microcontroller could not supply that amount of current\. To fix this problem, we added a transistor and used it as a switch to allow the 9V rail, connected directly to the external power supply, to power the speaker\. We decided to use a TIP31 transistor since it can dissipate a lot of heat and we tested several resistors in an effort to limit the current to 1A, eventually choosing to use a 1k resistor\.

Circuit schematic for the center controller

Circuit schematic for the timer unit

### PCB Layouts for the Center Console

Figure 11, on page 24, shows the layout for the final version of the center console’s PCB board\. Perhaps the most immediately noticeable features are the seven RJ45 Ports and one barrel jack connection on the bottom and left of the board, designed to stick out slightly when attached in order to pop out of the casing, allowing for easier connections\. Next to the barrel connector, there is a JST  connector which connects to a switch breakout board, shown in Figure 12, which serves to connect the power of the system to a switch, making it easier for the user to turn it off and on\. After passing through this switch, the 9V power input passes through a voltage regulator, giving us access to a 3\.3V source, which is used to power the external button boxes and one pin on each of the pin extenders\. Each of the six rightmost RJ45 Ports connects to one of these button boxes, and so each one takes four signal inputs: the 3\.3V power source, the ground connection, and dedicated signals to and from the microcontroller \(seen on the right side\) for the button input and LED output, respectively\. In order to avoid too much power accidentally being sent into the button boxes, the layout of signals on this port is intentionally different to that of the other six\.

The leftmost RJ45 Port is connected to the external timer unit, which requires more power to operate \(9V and 1A\), and as such this port has connections to the 9V source, the 3\.3V source, the ground connection, and the I2C SCK and SDA signals coming from the microcontroller \[13\] \[14\]\. A very similar setup can be seen to the left of the microcontroller, where a pin extender connects, through resistors, to the pins of two more JSTs, connecting in turn to the small timer breakout PCB \(seen in Figure 13\) which houses the small seven\-segment displays acting as the native timer unit on the center console itself\. The pin extender connects to the microcontroller using I2C, and in order to ensure that the signals coming from the SCK and SDA pins resulted in the same numbers being displayed on each timer unit, it was imperative that we connect the same segments to the same pins on the pin extender for each, despite the layouts being different on the seven segment units themselves\.

Near the top of Figure 11 can be seen six more JSTs: five of which connect to the on\-board buttons on the center console \(Start, Reset, Next, Timer Up, and Timer Down\) and the sixth of which connects the mode switch, attached to another switch breakout board\. The five buttons are approximately located where the JSTs are, but the mode switch is located closer to the RJ45 Ports, requiring longer JST wires\. In between the microcontroller and the Ethernet connectors are a resistor, TIP31 transistor, and a two\-pin JST\. These three components collectively act as the circuit for the speaker, connecting it to a pin of the microcontroller and giving it enough power to be audible from twenty\-five feet away from the system\.

Changes from this version of the center console PCB and the previous version, which was modified by hand and is currently being used by the system, mostly revolve around mis\-routed connections\. The ground trace was originally laid out incorrectly, making two unconnected grounds; the SDK and SDA pins were accidentally swapped with two digital I/O pins, and the microcontroller was originally unnecessarily close to the rightmost timer unit JST, making connection difficult\. These have all been fixed in this updated version, which should make future iteration much smoother\.

It should be noted, for anyone aiming to replicate this system, that due to some sort of glitch in the modelling software used, DesignSpark PCB Explorer, there were a couple of incorrect connections \(one between ground and the input voltage at the barrel jack and one between the 3\.3V rail and a pin on the microcontroller\) which we had to manually remove every time we worked with the PCB design \(and as such are not shown in Figure 11, below, as it might confuse anyone working with our files\)\.

Figures 12 and 13 show, as mentioned, the breakout boards for the switch and small timer unit, respectively\. The main purpose of these boards is to connect the JSTs on the main PCBs to components housed on the top of the unit, meaning they needed to be designed such that components could be soldered on either side without blocking the components themselves\. As such, the leads on the components needed to connect through a via to swap to the other side of the board and the JSTs needed to be far away from the component pins that they could be placed flush with the unit\.

The screw holes on these breakout boards fit \#6 machine screws and were designed specifically to line up with indents in the lids, such that the components pop out of the cases\. Furthermore, for anyone who may be looking at both this report and our system, the switch breakout board, specifically, is updated when compared to what is currently being used by the system, which is only a one\-sided board, and all three PCBs above share the same quirk of having overly long JST components compared to what is actually used \(four pins instead of three, for example\) due to a limited number of component models being available for use\.

Updated center console PCB layout

Updated switch breakout PCB layout

Small timer breakout PCB layout

### Center Console Enclosure

The center console’s enclosure, shown below in Figure 14, is a simple design that allows critical controls to be prominently displayed and indicated with embedded labels\. The enclosure features two cutouts for the mode and power switches and five cutouts for buttons as well as a built\-in speaker mount\. The lid is mounted to the base with four screws as well as a snap\-on design chosen to keep everything in place with more stability\. Interior components are screw\-mounted into the base or lid in such a way that they cannot be easily accessed by a casual user but can be opened by someone who knows what they’re doing\.

All screws used are 3mm machine screws, albeit of different lengths, as we wanted to standardize the screws we used for all parts of this design, from those used for mounting components to those used in securing the various lids\. Every mount makes use of four machine screws to secure breakout PCBs to the enclosure itself, with JSTs being used to connect these breakout boards back to the main PCB\. Less screws could likely have been used, but using four ensured a more durable, reliable design\.

Furthermore, access to the microcontroller is not readily available without removing the center controller lid, as this should not be necessary during the course of competition\. We used 3D modelling tool Fusion to generate STL files for 3D printing, which was both inexpensive and worked well with our 3D printer\. Furthermore, to add further support to the center controller and help give the box a better grip to surfaces so that Ethernet cables wouldn’t pull the system off a table, rubber tape was added on the underside the box\.

3D models of the center console enclosure

### Selection of Cables

We chose to use Ethernet cables in our design of this system\. This was done for three key reasons: Ethernet cables are durable, have a large number of internal wires, and are widely available\. Furthermore, while Ethernet cables may conventionally be more expensive than some other options, it is far from difficult to procure them at a reduced cost\. For example, we were able to purchase ten 25ft cables for $15 from a clearance sale\.

Since the center console must transmit signals to many different physically separated systems, it uses cables to connect to them\. Any cable with an RJ45 connector will work with our design, but as mentioned, we found Ethernet cables to be our best option\. The button boxes receive ground, 3\.3V, and LED signal inputs while transmitting one output to the center console to indicate whether or not the button is currently pressed\. The timer unit, on the other hand, has zero outputs and five inputs: ground, 3\.3V, 9V, and two signals to communicate with the timer unit’s pin extender using I2C: SDA and SCK\.

### Software Implementation

The general software architecture resembles a simple state machine\. There are two different modes the system can operate in, Type 1 Mode and Type 2 Mode, and each has a slightly different flow to its operation, as is displayed in Figures 15 and 16, below\.

As seen in Figure 15, while in Type 1 Mode, the system begins in the “Idle” state\. In this state, the user cannot buzz in, the timer does not count down, and the operator can adjust the length of the timer\. Once the start button is pressed, the system transitions into the “Running” state, whereupon the user can no longer adjust the timer length, the timer counts down, and the contestants can buzz in\. If a contestant buzzes in, the system transitions to the “Buzz” state\. This is identical to the Running state, except that other contestants cannot buzz in\. If the Next To Answer button is pressed, the system transitions back to the Running state, the LED is turned off, and that contestant can no longer buzz in\. If the timer reaches 0 in either state, the system transitions to the “Stalled” state\. Here, the timer continues to display zero, no contestants can buzz in, and the LEDs are frozen in their current state\. If the reset button is pressed in any state, the system transitions back to the Idle state\. During the transition, the timer and all the LEDs are reset \(i\.e\. the timer will display the preset initial value and all LEDs will be turned off\)\.

Type 1 Mode state machine

Type 2 Mode, shown in Figure 16, is very similar to Type 1 Mode, being identical in the Idle, Running, and Stalled states\. The only difference is the Buzz state, in which other contestants can buzz in and will be added to a queue of buzz\-ins\. If the Next To Answer button is pressed and there are more buzz\-ins in the queue, the system will remain in the Buzz state, but the previous contestant’s LED will turn off and the next contestant’s LED will turn on\. If there are no answers in the queue, the system will transition back to the running state\.

Type 2 Mode state machine

## Design and Implementation of the Button Boxes

The button boxes are what the contestants use to buzz in\. They feature a simple design with an LED and button on the top, as well as an RJ45 Ethernet port on the back\. Up to six of these units can be connected to the center console at once, with them all receiving power through the RJ45 port\. An image of an individual button box is displayed below, in Figure 17\.

A button box

### PCB Layouts for the Button Boxes

Figure 18, below, shows the layout of the updated button box PCB\. The JST on the left connects to the button itself, with one connection to ground and another to the RJ45 Port on the right, such that the signal of the button, having been pressed, can make its way back to the microcontroller in the center console\. To the right of this are the transistor and seven resistors, which serve to constrain the voltage coming into the LED, which itself is connected through an eight\-pin JST to another breakout board, as shown in Figure 19\.

The LED we purchased is essentially six LEDs in parallel, all within one LED casing, which is why six identical paths are all shown next to each other in the figures \(each of which need a connection to ground in the neighboring pin, as shown in Figure 19\)\. As with the center console PCB, the RJ45 Port is designed to stick out slightly from the casing, to make it easier to plug in\. Furthermore, this design is, again, slightly updated when compared to what is in the first version of the system’s button boxes, as the pin arrangement for the transistor was initially mislaid \(BCE instead of EBC\), requiring the simple fix of the leads being twisted around slightly \(with a heat sink put over them to be safe\)\. Furthermore, the top\-rightmost via on the LED breakout board is simply there to denote which via corresponds to pin one on the LED, which is, unusually, in the top right corner of the component\.

Updated button box PCB layout

LED breakout PCB layout

### Button Box Enclosure

The button box enclosure, shown below in Figure 20, has been designed in a similar manner to the center controller enclosure, but with more of a focus on size, such that they are small enough to be easily handheld while being large enough that they can be placed on the table without falling off\. They were also designed with the goal of being printed with filament and so, due to the light nature of this material and the relatively heavy weight of the Ethernet cables, rubber tape was added to the bottom of the button boxes to give them a better grip on the surface upon which they will be placed\. The design also features rounded edges on the bottom to be more comfortable to hold and use\.

The final size was larger than we had originally estimated, but this has not affected performance and further allows the button boxes to sit on standard tables without being pulled off by the connected Ethernet cables\. The lid of each button box unit is secured with two screws on the front as well as a latch on the interior, requiring the bottom of the lid to be slid into place first\. Internal mounting of the LEDs is done with breakout boards and 3mm screws just as with the center controller, however the buttons are secured directly to the lid and do not need any breakouts boards\. These mounted components use JSTs on their breakout boards to connect back to the central PCB\. These units should also not have to be opened by a user or operator for any reason since the button and RJ45 port are all that need to be used by our stakeholders\.

3D models of the button box enclosure

## Design and Implementation of the Timer Unit

The external timer unit, seen in Figure 21, below, is designed to mirror the embedded timer display of the center console\. It is connected and powered through a designated RJ45 port on the center console \(the leftmost of such ports\) and uses a pin extender utilizing I2C, giving it a range limitation of about two feet of cable in distance from the center console\. The seven\-segments are large enough to be viewed easily from at least twenty feet\.

The timer unit

### PCB Layouts for the Timer Unit

Figure 22, below, shows the updated layout for the external timer’s PCB, which connects via JSTs to the large seven\-segment displays \(connected to the large timer breakout boards shown in Figure 23\) at a 90\-degree angle\. The trace layout of this board is noticeably more complex and sprawling than that of previous boards; this is both to ensure that the PCB is single\-sided \(for ease of printing\) and so that the pins in the pin extender \(in the center of the PCB\) connect to the same segments of the large seven\-segment displays as with the small ones, as described above in the center console PCB description\.

Once again, the RJ45 Port is designed to stick out slightly from the end of the PCB for easier cable insertion, and the extra via on the breakout board exists to indicate pin one\. Furthermore, the external timer unit board shown below in Figure 23 is slightly updated compared to what is currently being used in our unit—this time in order to fix an issue of two segments being swapped—and the JSTs are far to the side of the large timer breakout board so as to fit them underneath the frame of the seven\-segment display itself\.

Updated external timer PCB layout

Breakout PCB layout for large seven\-segment display

### Timer Unit Enclosure

The timer unit enclosure, as shown in Figure 24, was designed similarly to the button box and center controller\. Fusion was originally used to create an STL file, and after some refinement, the enclosure itself was printed using 3D printing filament\. The overall size was reduced from earlier prototypes to give a more unique look to the timer\. There is a single opening on the back for an RJ45 Port, while on the front the double seven\-segments are displayed prominently\.

The seven\-segments are mounted with the same 3mm screws as the rest of the casing designs, as is the lid, and there is an additional snap\-on component for the rounded portion for a more secure fit\. There is little extra space in this container due to the reduced size, but this allows it to have a much more refined look\. Furthermore, to add extra grip to the timer unit, rubber tape has been added to it as well, though it is less necessary than with the previously discussed units due to the relatively light weight of the seven\-segments\. This unit should also not have to be opened by any stakeholder\.

3D models of the timer unit enclosure

## Design of the Bluetooth GUI

Our Bluetooth requirement was self\-imposed, added to this project to give the user more control over the system behavior without needing to directly modify the on\-board code, as well as to allow them to control the center console remotely\. The GUI included as part of the final product is shown below in Figure 25\.

The Bluetooth connection itself is made with an internal chip on the center console’s microcontroller and a connection is easily established with the use of the GUI’s connect button, so long as you are within 30 feet of the center console \[13\]\. The interface was designed via a python executable, so as long as the user’s selected device has support for python \(as most modern computers do\), the GUI application can be run on it\. The application was designed using several key libraries including Tkinter for the GUI and Bleak for establishing and maintaining a Bluetooth connection\.

Each device is given a unique thirty\-one byte code to ensure that multiple devices will not interfere with each other if they run on the same network\. The GUI layout mirrors the layout of the center controller console with the addition of a text input field that allows for specific system variables to be changed, including specific initial time values, different clock tick\-down rates, and buzzer tones\. A status indicator is located at the top of the display to give the connection state when connecting for the first time \(although do note that it does not reflect disconnection after the first connection, so if a given command is stuck at sending, a reconnection will be necessary\)\.

The Bluetooth GUI

## Verification of Requirements

In order to verify that our system not only met both the requirements which our clients had set out for us, but also simply worked at a level we were not just satisfied with, but proud of, it was necessary for us to create a thorough set of tests to put the whole system through\. Specifically, we aimed to test the nine aforementioned key features: affordability; functionality; transportability; durability; and proper design of the center console, button boxes, Ethernet cables, timer unit, and GUI option\. As can be seen in Table 4, below, we have compiled these tests, the methods which we used to run them, their results, and what those results meant for the success of the unit under said tests\.

Verification of requirements

|Features|ID|Requirements|Verified By|Results|Achieved?|
|---|---|---|---|---|---|
|Ethernet Cables, Transportable|E\-1|Ethernet cables are at least 25ft long\.|Measured unspooled Ethernet cable with a tape measurer\.|The cable for the timer unit is only 1\.5 feet\. All other cables meet the requirement\.|N|
|Ethernet Cables, Transportable|E\-2|Ethernet cables must be coil\-able\.|Coiled Ethernet cables around hand, uncoiled them, and made sure the system still worked\.|Cables determined to be easily coilable\.|Y|
|Ethernet Cables|E\-3|Ethernet cables must adhere to T568B standard for RJ45 ports\.|Made sure to pick out cables that explicitly follow this standard for our purchased Ethernet cables\.|Failed to buy cables which met this explicitly\.|N|
|Affordable|A\-1|Total unit cost must be below $200\.|Maintained a BOM and kept track of costs for parts used in final design\.|Total unit cost: $97\.02 \(explained more below\)\.|Y|
|Affordable|A\-2|Total development cost must be below $300\.|Maintained a BOM and kept track of costs for parts not used in final design\.|Total development cost = $277\.21|Y|
|Durable|D\-1|System must be able to withstand 100 button presses with at least a 0\.5s gap between them\.|Rapidly pressed each button 100 times, making sure that the gap between presses was at least 0\.5s by using a stopwatch\.|No issues with system whatsoever after 100 presses\.|Y|
|Durable|D\-2|System must be fully functional after a drop of 3 inches\.|Measured the distance between a table and the center console using a ruler, then dropped it and tested functionality\.|No issues with system whatsoever after this drop\.|Y|
|Durable|D\-3|System must be fully functional after a being shaken for one minute\.|Picked up the center console, shook it for one minute \(measured using a stopwatch\), and tested functionality\.|No issues with system whatsoever after shaking\.|Y|
|Durable, Ethernet Cables|D\-4|System must be fully functional after Ethernet cables have been stepped on by someone weighing over 150lbs\.|Someone whose weight was known to exceed this stepped on the cables, then functionality was tested\.|No issues with system whatsoever after stepping on cables\.|Y|
|Transportable, Center Console|T\-1|Center console must be smaller than 14’’ x 12’’ x 5’’\.|Measured lengths of sides using a ruler\.|Center console is 7’’ x 4\.5’’ x 2\.75’’\.|Y|
|Transportable, Center Console|T\-2|Center console must be lighter than 15lbs\.|Measured using a scale obtained from a chemistry lab\.|Center console is 0\.99lbs\.|Y|
|Transportable, Button Boxes|T\-3|Individual button box must be smaller than 3’’ x 2’’ x 1\.5’’\.|Measured lengths of sides using a ruler\.|Button boxes are each 4’’ x 2\.8’’ x 1\.5’’\.|N|
|Transportable, Button Boxes|T\-4|Individual button box must be lighter than 2lbs\.|Measured using a scale obtained from a chemistry lab\.|Button boxes are each 0\.24lbs\.|Y|
|Functional, Button Boxes|F\-1|During normal operation, correct button box must light up and sound must play from center console upon button press 100 times out of 100\.|Tested this by hand with all six button box positions\.|Correct operation every time this was tested\.|Y|
|Functional, Center Console, Timer Unit, Button Boxes|F\-2|Reset button resets and stops both timers, as well as clears all lights 30 times out of 30, regardless of what else is happening in system’s operation\.|Tested this by hand, making sure to test with each button box having been lit up\.|Correct operation every time this was tested\.|Y|
|Functional, Center Console, Timer Unit, Button Boxes|F\-3|Start button starts timers, counting down from initial timer length value and allowing for buzz\-ins, 30 times out of 30\.|Tested this by hand, with at least 20 different starting values for the timer\.|Correct operation every time this was tested\.|Y|
|Functional, Center Console|F\-4|Mode switch correctly checks which mode the system should be playing in when Start button is pressed, and correctly causes that mode to be implemented for that round, 12 times out of 12\.|Tested this by hand, switching mode at various times throughout a round and verifying that mode did not change until new round started\.|Correct operation every time this was tested\.|Y|
|Functional, Center Console, Button Boxes|F\-5|In Type 2 mode, pressing Next when a contestant had buzzed in \(causing their LED to light up\), turns off the currently\-on LED and turns on that of the first contestant \(who hadn’t yet buzzed in for that round\), playing the “next answer” sound, 12 times out of 12\. Additionally, if no more contestants had buzzed in, turns all LEDs off and allows all players to buzz in again 12 times out of 12\.|Tested this by hand, making sure all 6 button boxes were both the one currently buzzed\-in and the first to buzz\-in when another “contestant” was answering at least once\.|Correct operation every time this was tested\. This is described more below\.|Y|
|Functional, Center Console, Button Boxes|F\-6|In Type 1 mode, pressing Next turns all LEDs off and allows all players to buzz in again 21 times out of 21\.|Tested this by hand, making sure all 6 button boxes were buzzed\-in when this was pressed at least once\.|Correct operation every time this was tested\. This is described more below\.|Y|
|Functional, Center Console, Timer Unit|F\-7|Pressing Timer Up increases the timer’s starting point by 5s, unless already at 95\.|Tested by hand while in idle state\.|Correct operation every time this was tested\.|Y|
|Functional, Center Console, Timer Unit|F\-8|Pressing Timer Down decreases the timer’s starting point by 5s, unless already at 10s\.|Tested by hand while in idle state\.|Correct operation every time this was tested\.|Y|
|Functional, Center Console, Timer Unit|F\-9|Both Timer Up and Timer Down must only have an effect when system is in idle state\.|Tested by hand when in idle and non\-idle states\.|Correct operation every time this was tested\.|Y|
|Functional, Center Console, Timer Unit|F\-10|Buzz\-ins will not work after timer reaches 0, but LED must stay on until Reset is pressed\.|Tested by hand with each button boxes being lit up and being unable to buzz in at least once\.|Correct operation every time this was tested\.|Y|
|Timer Unit|U\-1|After pressing Start, timer units tick down at a rate accurate to actual time within 20ms\.|Used a stopwatch to compare time on timer displays with actual time elapsed for several different periods of time\.|Not only was this accurate, we even added an extra feature to the Bluetooth module to allow the user to change the clock speed as they wished\.|Y|
|Timer Unit, Center Console|U\-2|Signals coming from the center console and going to the timer unit beyond just ticking down \(e\.g\., being reset to the initial value\) must be reflected without 20ms of the button press\.|Hooked an oscilloscope to the Reset button on center console and a segment on a display on the timer unit and measured the time difference between pulses after a press\.|Time difference measured using oscilloscope was below 20ms\.|Y|
|Button Boxes, Center Console|B\-1|Inputs from the button boxes must be reflected in the center console \(e\.g\., a button press causing the speaker to sound off\) within 10ms of a button press\.|Hooked an oscilloscope to the button on a button box and the speaker pin on the center console and measured the time difference between pulses after a press\.|Time difference measured using oscilloscope was below 10ms\. This is described more below\.|Y|
|Button Boxes, Center Console|B\-2|Signals originating from the center console and going to a button box \(e\.g\., Reset clearing and LED\) must be reflected within 20ms of a button press\.|Hooked an oscilloscope to the Reset button on center console and the button on a button box and measured the time difference between pulses after a press\.|Time difference measured using oscilloscope was below 20ms\.|Y|
|Button Boxes, Center Console|B\-3|Signals originating from button boxes but passing through the center console before being reflected on that button box \(e\.g\., the LED lighting up upon a button press\) must be reflected within 20ms of a button press\.|Hooked an oscilloscope to the button on a button box and the LED of said button box and measured the time difference between pulses after a press\.|Time difference measured using oscilloscope was below 20ms\.|Y|
|Center Console|C\-1|Three sounds, at least 100Hz apart, must play in three different scenarios: being first to buzz in, being next to buzz\-in \(following a Next press\), and the timer reaching 0\.|Used OnlineMicTest\.com to distinguish frequencies of different sounds\.|First buzz is 429Hz\. Next answer is 322Hz\. Time end is 645Hz\.|Y|
|Center Console|C\-2|All three sounds played by the buzzer must be at least 50 decibels at a range of 25ft\.|Used the app Decibel Pro for the iPhone to determine this\.|First buzz is 57db\. Next answer is 59db\. Time end is 55db\.|Y|
|Center Console|C\-3|All three sounds played by the buzzer must be between 220Hz and 1480Hz\.|Used OnlineMicTest\.com to distinguish frequencies of different sounds\.|First buzz is 429Hz\. Next answer is 322Hz\. Time end is 645Hz\.|Y|
|Center Console, Gui Option|C\-4|Bluetooth connection must be possible at a 30ft distance from the center console, given that at least 30s have passed after power on\.|Measured out the distance from the center console and tested this with a laptop\.|Reliable connection was established from this distance\.|Y|
|Gui Option|P\-1|Bluetooth connection between laptop and center console must last for at least 2 hours\.|Used a stopwatch after connection was established\.|The Bluetooth connection is maintained while laptop and center console are within range\.|Y|
|Functional, Gui Option, Timer Unit, Button Boxes|P\-2|The Reset Timer button, pressed on the GUI, must reset and stop the timers and clear all lights 6 times out of 6, regardless of what else is happening in the system’s operation\.|Tested with all six button boxes having been on\.|Correct operation every time this was tested\.|Y|
|Functional, Gui Option, Timer Unit, Button Boxes|P\-3|The Start Timer button, pressed on the GUI, will start the timers, counting down from whatever the initial timer length value is, and allow for buzz\-ins 5 times out of 5\.|Tested with five different values of initial time on the timer\.|Correct operation every time this was tested\.|Y|
|Functional, Gui Option|P\-4|2 times out of 2, the Change Mode switch, toggled in the GUI, will be checked whenever the start timer button is pressed, and for the round, behaviors will act accordingly, as long as the Change Mode switch was changed at least 100ms before the start button was pressed\.|Tested in both directions \(2\-\>1 and 1\-\>2\)\.|Decided against implementing this feature in the end, conflicts with built in switch\.|N|
|Functional, Gui Option, Button Boxes|P\-5|5 out of 5 times, the Next Answer button, when pressed in the GUI, will, while in Type 2 mode, turn off the current contestant’s LED and turn on that of the next contestant, while playing the secondary buzz\-in sound\. If no more contestants had buzzed in, this button will act as it does in the Type 1 mode and turn off all LEDs while re\-enabling buzz\-ins\.|Tested with 4 different button press combinations\.|Correct operation every time this was tested\.|Y|
|Functional, Gui Option, Button Boxes|P\-6|7 out of 7 times, the Next Answer button, when pressed in the GUI, must turn all LEDs off and allow for new buzz\-ins when in Type 1 mode\.|Tested with 7 different button press combinations\.|Correct operation every time this was tested\.|Y|
|Functional, Gui Option, Timer Unit|P\-7|5 out of 5 times, the Increase Timer Length button will increase the timer’s start point by 5 seconds \(unless already at 95s\)\.|Tested with 5 different initial timer values\.|Correct operation every time this was tested\.|Y|
|Functional, Gui Option, Timer Unit|P\-8|5 out of 5 times, the Decrease Timer Length button will decrease the timer’s start point by 5 seconds \(unless already at 10s\)\.|Tested with 5 different initial timer values\.|Correct operation every time this was tested\.|Y|
|Gui Option, Button Boxes, Center Console|P\-9|Signals originating from center console, but selected via the GUI, and going to the button boxes will register in under 1\.02s\.|Tested through observation\.|Bluetooth commands were reflected much quicker than this\.|Y|

As can be seen, we overwhelmingly met our requirements, meaning that the system is fully functional, both when operated using either the physical buttons or the GUI; reasonably durable and transportable, as is required when moving between competitions; and is affordable for competition hosts to have constructed\.

Of the requirements that were not met, E\-3 was likely actually met, in that the Ethernet cables worked as we expected it to, but we failed to explicitly buy cables that met this standard; T\-3 ended up being unreasonable, as the casing needed to be large enough to actually fit the LED, button, and RJ45 Port; and P\-4 was intentionally not met as implementing the mode switch into the GUI would make the labels on the center console too confusing\.

In order to demonstrate more how we ran these tests and collected the relevant data, we have highlighted the tests for four of the aforementioned requirements—A\-1, F\-6, F\-5 and B\-2—and described in more detail how we arrived at our results in the four subsections below\.

### Verification for Unit Cost \(Requirement A\-1\)

Requirement A\-1, as described in Tables 5 and 6, below, is the affordability requirement for the production of one unit\. In order to guarantee that the system is easy to further iterate on in future, as well as that such a system will not require disproportionate investment by our clients, we sought to create a finalized system costing less than two\-hundred dollars to produce when using, as we did, on\-campus PCB milling and 3D\-printing services to drive down the price\. Table 6, is the BOM for the finalized unit, taking into account only the components that actually ended up being used and dividing larger packages of components purchased by the number used for constructing one unit \(for example, the price of the microcontrollers we bought was divided by three, as we purchased a three\-in\-one package\)\.  As the table shows, the final cost was $97\.02—well below the requirement\.

Development cost

|7\-Segments|40\.44|
|---|---|
|Ethernets|24\.95|
|Arduino|78\.57|
|Buttons|8\.99|
|Potentiometer|9\.99|
|RJ45 Ports|17\.98|
|DC Regulators|9\.97|
|Speakers|20\.17|
|Rubber Pads|11\.97|
|LEDs|25\.92|
|Wall Adapter|35\.28|
|Pin Extender|10\.14|
|Power Switch|5\.82|
|RJ45 Breakout|16\.8|
|Shipping / Taxes|57\.24|
|Total|374\.23|

Unit cost

|Arduino|27|
|---|---|
|Buttons|8\.99|
|LEDs|8\.64|
|Small 7seg|2\.66|
|Big 7seg|9|
|Pin ext|3\.38|
|Switch|3\.88|
|Speaker|14\.99|
|Wall Plug|8\.49|
|Regulator|9\.99|
|Total:|97\.02|

### Verification for Type 1 Mode Accuracy \(Requirement F\-6\)

Next, we move on to requirement F\-6: that the operation of the system under Type 1 Mode works as intended\. To accomplish this, we hooked up six button boxes to the center console\. When the mode switch was flipped to Type 1, we tested pressing Next To Answer with each possible ordering of button box LEDs lit up \(including none\)\. There are seven possible orderings, so we tested each twice with all six boxes plugged in, then once with only one box plugged in, to verify that this works with different combinations of button boxes plugged in\.

Completing this test required the center console unit, a 9V/18W power supply with a barrel plug \(included with the unit\), six button box units, and six 25ft Ethernet cables in order to connect the button boxes to the center console\. First, we plugged the 9V barrel plug into the power jack and flipped power switch to the “on” position\. We then plugged all six button box units into separate Ethernet cables, then connected each cable end to a separate port of the center console \(note: we made sure not to plug into the port closest to the power jack, as that that is for the timer unit and cannot be used for a contestant box\)\.

After verifying that the mode switch was in the “Type 1” position, we pressed the Reset Timer button, then the Start Timer button to begin operation\. Within 20 seconds of pressing Start Timer, we pressed the button on the first button box such that the LED lit up, then pressed the Next To Answer button\. Finally, we pressed the Next To Answer button and verified that this caused the LED of the first button box to turn off\. If it did, we recorded a “pass”, and if not, we recorded a “fail\.”

Following this, we pressed the Reset Timer button, then the Start Timer button, and repeated the previous steps for each button box, two times each\. This done, we unplugged all but one Ethernet cable from the center console and again repeated the test, now with each box being isolated\.

To pass this test, we expected a 100% success rate for all trials\. In other words, we would not be satisfied with this requirement’s performance unless every iteration of the test resulted in a “pass”—a “pass” in this experiment being given when a firm button press which fully compressed the button until it could not be pushed in any further resulted in an LED state change as described above\. Table 7, below, shows the results from this test, with trials 1\-12 involving all six button boxes and trials 13\-24 only involving one at a time\.

Requirement F\-6 verification results

|Trial|Pass/Fail|Trial|Pass/Fail|
|---|---|---|---|
|1|P|13|P|
|2|P|14|P|
|3|P|15|P|
|4|P|16|P|
|5|P|17|P|
|6|P|18|P|
|7|P|19|P|
|8|P|20|P|
|9|P|21|P|
|10|P|22|P|
|11|P|23|P|
|12|P|24|P|

As can be seen, the next answer functionality worked perfectly in Type 1 Mode for every trial\. This verified that the Next To Answer button worked in this mode with varied button boxes plugged in to the center controller\. The margin of error for this experiment was 0 due to the 100% pass requirement, and as every test was a success, F\-6 was deemed to have passed\. We were therefore confident that this feature would work reliably in the competition regardless of the number of participants\.

### Verification for Type 2 Mode Accuracy \(Requirement F\-5\)

Just as with requirement F\-6, we tested Requirement F\-5 by starting with all six button boxes plugged in, first verifying functionality for each box’s LED to be the one on at first, then switched off when the Next To Answer button was pressed\. Second, we tested the mode’s functionality with each box’s LED being the one to be turned on by being next in the queue when Next To Answer was pressed while another box’s LED was currently on \(this having been tested twice, with two different originally\-on LEDs\)\. Finally, we tested the mode with each box’s LED being turned off with no other LEDs turning on due to no other “contestant” having buzzed in\. Following this, we tested the results of pressing the Next To Answer button with each button box being the only one plugged in, with us expecting them to function as they did under Type 1 Mode, and also completed a few trials in which we pressed Next To Answer with no button boxes plugged in, just to make sure that nothing broke under this edge case\.

The equipment used for these tests was the same as those required for the previous requirement:the center console unit, a 9V/18W power supply with a barrel plug \(included within the unit\), six button box units, and six 25ft Ethernet cables\. Furthermore, no additional setup was needed, as we were able to perform these tests immediately after those for Requirement F\-6\.

After flipping the mode switch to the “Type 2” position, we pressed the Reset Timer button, then the Start Timer button to begin operation\. Within twenty seconds of pressing Start Timer, we pressed the button on the first button box such that the LED lit up, then pressed the button on any other button box \(which one wasn’t important for this step\)\. Next, we pressed the Next To Answer button and verified whether or not the LED on the first button box turned off, making sure to record this result\.

We did this with each button box having been the first to be lit up\. Following this, and after again having pressed the Reset Timer button, then the Start Timer button, within twenty seconds we pressed the button of any button box but the one being tested, then pressed the button on the tested button box\. In each trial, we expected the LED of the first button box to turn off and the LED of the other button box \(the button of which had been pressed second\) to turn on\.

This was repeated to test all six button boxes twice each\. Penultimately, we unplugged all but one button box, verifying twice for each port that pressing Next To Answer resulted in an “on” LED turning off, just as in Type 1 Mode\. Finally, just as an edge case, we pressed Next To Answer six times with no button boxes plugged in, just to make sure that nothing went wrong\.

Table 8, below, shows the results from this thorough testing of the Type 2 Mode\. Tests 1\-6 were tests for cases where a second contestant had not answered and the next answer button was pressed\. Tests 7\-18 tested that every contestant could be cycled to if there was already an active contestant LED\. Tests 19\-30 verified that the next answer button would still behave as expected if only a single contestant box was plugged in\. Finally, tests 31\-36 tested the functionality with no button boxes plugged in\. To verify requirement F\-5, we expected every test to pass\.

Requirement F\-5 verification results

|Trial|Pass/Fail|Trial|Pass/Fail|Trial|Pass/Fail|
|---|---|---|---|---|---|
|1|P|13|P|25|P|
|2|P|14|P|26|P|
|3|P|15|P|27|P|
|4|P|16|P|28|P|
|5|P|17|P|29|P|
|6|P|18|P|30|P|
|7|P|19|P|31|P|
|8|P|20|P|32|P|
|9|P|21|P|33|P|
|10|P|22|P|34|P|
|11|P|23|P|35|P|
|12|P|24|P|36|P|

As can be seen, just as with Requirement F\-6, above, the next answer functionality worked perfectly for Type 2 Mode in every trial, confirming that the Next To Answer button will work in either mode or with varied button boxes plugged in to the center controller\. The margin of error for this experiment was 0 due to the 100% pass requirement, as every test was a success\. We can therefore be confident that this feature will work reliably in the competition regardless of the number of participants\.

### Verification for Response Time \(Requirement B\-5\)

In a further effort to verify the functionality of the system, we created Requirement B\-5 in order to ensure that all center console controls showed results at the contestant button boxes in a timely manner\. Signals coming from the center console to a button box aren’t as urgent as those going the other way, since they aren’t inherently timing\-dependent like buzzing\-in is, but they still need to be reflected in the LEDs quickly enough that the competition can run frictionlessly\.

This was tested using the Next To Answer button, enabling us to use an oscilloscope to show that the time from the center controller receiving an entry to a button box registering an output is less than 20ms\. In more detail, we first hooked up an oscilloscope to the test vias in the signal line of the Next To Answer button on the center console PCB and the LED input line on a button box PCB\.

After ensuring that the system was in the “running” state and that the button on said button box had been pressed \(i\.e\., its LED was on\), we pressed the Next To Answer button\. Regardless of the current mode of operation, with no other button box buttons having been pressed at this point, pressing the Next To Answer button was expected to cause this LED to turn off\. Therefore, we were able to measure the time between the press of the next answer button and the LED signal going low\. To pass this requirement, we aimed to be confident that the elapsed time between these two slopes was less than 20ms\.

This test required an oscilloscope \(we used a Tektronix MDO4014B 100MHz\), the center console unit, two button box units, two 25ft Ethernet cables, a 9V/18W power supply with a barrel plug, and a flash drive in order to store the output of the oscilloscope\.

We began with the center console unit and button box removed from their 3D printed housing \(in this case, we had the components arranged on breadboards\), making sure that the power switch was flipped to the “off” position\. We connected probe 1 of the oscilloscope to the Next To Answer line on the breadboard version of the center controller and connected probes 2 and 3 to the LED on the breadboards for Button Box 1 and 2\. After setting a falling edge trigger on the oscilloscope at 2V for probe 2, we plugged the power supply into the center console and used both Ethernet cables to connect the button boxes to the center console\. We then turned the power switch on, ensured the built\-in timer display powered on, and pressed the Timer Up button until the center console displayed 60 \(meaning that the system would tick down for sixty seconds\)\.

After pressing the Start Timer button on the center console, the timer began to tick down\. While this was happening, we pressed the contestant input button on Button Box 1, then pressed the same button on Button Box 2\. Probes 1 and 2 now read a high voltage of 3\.3V and probe 3 was low\. Next, we pressed the Next To Answer button on the center control unit, causing probe 1 to trigger\. On the oscilloscope, we added a marker to where the falling edge of probe 1 reached 2\.7V the first time, then added another marker to where the rising edge of probe 4 reached 2\.7V\. Finally, we added a delta time measurement for the difference between both markers and recorded the screen of the oscilloscope, saving the image to the flash drive\.

If the time difference was less than 20ms, this test was considered to have been successful\. This being determined, we pressed the Reset Timer button on the center controller and repeated these same steps, this time pressing Button Box 2 first and recording the difference between the falling edge of probe 1 and the rising edge of probe 2\. If this test was successful, we expected probes 1 and 3 to read a high voltage, and probe 2 to be low\. This final test was then repeated a second time, in order to make sure our results were accurate\.

Table 9, Figure 26, and Figure 27, below, show the results of these tests, demonstrating the system response time to system control commands input via control buttons\. In order to verify Requirement B\-2, every measurement in Table 9 was expected to show a timing difference of less than 20ms\. Figure 26 shows the first timing difference test for contestant Button Box 1 between the first press of the Next To Answer button and when the system responded by disabling the light of the box with a lit LED\. A successful test was expected to show a delta t measurement of less than 20 ms\. This result was further recorded in Table 9\.

Similarly, Figure 27 shows the second timing test for the Next To Answer button\. This test differed from the previous in that a different button box was now being used, such that the same timing and behavior could be observed with multiple different contestant button units\. This test was also expected to show a delta t measurement of less than 20 ms, and was recorded in the second column of trial 1 in Table 9\. We did not include the oscilloscope outputs of trials 2 and 3, as they were essentially the same as those of trial 1\.

As can be seen in Figures 26 and 27, the delay is, as we aimed for, below 20ms in every test\. As is seen in Table 9, this is a consistent result for all button boxes, ensuring operation of this system should be smooth and reliable\. The delay in all trials was approximately 11 ms, meaning we passed Requirement B\-2 with a margin of 9ms\. This margin could be increased if necessary since we presently have a 10ms count in the code on press as a button debounce measure, however, the system as it should be more than satisfactory for this system requirement\.

Requirement F\-5 verification results

||Contestant 1 first time delay \(ms\)|Contestant 2 first time delay \(ms\)|
|---|---|---|
|Trial 1|11ms|11ms|
|Trial 2|11ms|11ms|
|Trial 3|11ms|11ms|

Trial 1, Contestant 1 results

Trial 1, Contestant 2 results

# Project Plan and Timeline

This section serves as a reference for the timeline of the project development\. The following sections are included to show the initial plan for the project with a comparison to how the development process actually worked out\.

## Planned Timeline

Our initial timeline for this project, laid out very early on and shown in Figure 28, below, was designed somewhat in the dark, mostly centered around an initial deadline of February 1st 2025 \(MATHCOUNTS competitions occur in February\) and the schedule for our senior design classes \[7\]\.

The planned project timeline

### Milestones of the Initial Timeline

Milestone 1:

Develop research memos for critical systems

Make part selections

Order parts

Milestone 2:

Use jumpers to setup hardware model for 6 buttons/LEDs

Develop code for basic button system

Use Ethernet cables to connect center breadboard to led/button breadboards

Milestone 3:

Software for timer module

Breadboard mockup for timer module

System commands, “start, reset”

Milestone 4:

Mode switch

“Next Answer” functionality

Design and Order PCBs

Milestone 5:

Verify PCBs

Solder Parts to PCB

Milestone 6:

Design and print 3D parts

Verification of soldered system

Milestone 7:

Mount components to 3D enclosures

Final system checks and verifications

## Implemented Timeline

The actual implementation for the project, shown in Figure 29, below, differed greatly from what had originally been planned or proposed, with even the structure of the initial milestones being largely lost as the project progressed\.

The first large difference from our initial plan was that our first step ended up being to create a complete working breadboard model instead of adding it piecemeal as other parts were completed\. We found that it was difficult to test separate parts without knowing how well it would all work together, causing us to implement all system commands at roughly the same time\. Additionally, we found that we couldn’t use RJ45 ports without soldering miniature premade breakout boards for them, further altering our plans\. While these changes did not cause a delay on their own, it did show us how quickly plans can change when the actual implementation begins\.

The second divergence from our initial plan was with the timer unit implementation, as we encountered an error with our initial seven\-segment displays and were forced to order replacements\. Unfortunately, this led to some problems with the required power supply since we then discovered that the displays would require more than a 5V source, further throwing a wrench into our earlier plans\. Due to this delay, the PCB was started with an incomplete model, requiring many revisions as the project progressed\. This caused the timer unit to get pushed back further than expected, and since it was required for the complete breadboard model, this delay propagated\.

The third major problem was related to the 3D enclosure design for the center console\. Midway through the project, a team member dropped out and did not leave their files behind, forcing us to restart the models from scratch\. This team member had been missing meetings from the start, which was why they were given a relatively small part in the form of 3D enclosure in the first place, although unfortunately we didn’t anticipate their complete withdrawal from the project\. This led to further PCB delays as testing had to be put on hold temporarily to finish the enclosure design\.

The fourth divergence came in the form of a decision to add an extra feature in the form of a Bluetooth GUI to send commands wirelessly\. Work on this was mostly completed when other parts of the project were stalled, but it still added a bit more labor\. It was a good insight into how last\-minute changes can be added to and influence a project, however\.

The fifth and final major divergence was caused due to simple PCB issues\. Some of these appeared after printing, calling for some quick fixes and schematic revisions\. When combined with other issues, primarily that of the delayed 3D printed enclosures mentioned earlier, this led to a complete project completion that was two weeks behind what we had anticipated\. While the delay wasn’t too extreme, especially since we were already a month ahead of the usual senior design project schedule, this all went to show us how small issues can snowball into problems that lead to overall delays or rises in the workload\. If we were to go back and do it all again, however, we could likely do the project much quicker since we now have more experience with the design process and with potential problems we should watch out for\.

The implemented timeline

# Budget

Our initial budget was $500 for total development costs, including $200 for the final unit\. Figure 30, below, shows our total development cost was $374\.23 and our total unit cost was $97\.02\. These numbers are both well within our budget\. See Section 2\.6\.1 for full tables of item costs\. Labor costs were not accounted for in the budget, but Figure 31, below, shows that the total labor cost, were this a professional operation, would have been $9300\.

Besides labor, the main expense in our project ended up being microcontrollers\. We bought three microcontrollers at $26\.19 each, resulting in a total cost of $78\.57, which was about a fifth of our final total development cost\. Aside from this, there was no other single expense that was significantly larger than the others; everything else was around the same price, with the overall system cost essentially coming from a lot of miscellaneous parts needed to build the system\.

These are several ways costs could have been reduced and, in general, cheaper components could have been bought for a large portion of the system\. However, the budget was large enough that price was not a concern so we did not optimize for cost and instead bought components that we were confident would work\. We could also have reduced labor costs by being more experienced, since we spent longer on development than we would have if, for example, we did this project now, with all that we now more about the design and manufacturing process\.

Physical expenses

Labor costs

# Recommendations

We are very satisfied overall with the outcome of this project in terms of meeting the requirements our clients set out for us\. Of the stakeholder features we set out for ourselves at the beginning—affordability, durability, functionality, transportability, center console operations, button box operations, timer unit operations, and GUI operations—we were able to meet all seven with no major complications\. As discussed in Section 2\.6 above, almost every requirement was met with flying colors, with the few not to pass not materially affecting the quality of the system\. In particular, the accuracy\-based requirements had no issue, meaning there should be no occurrences of mis\-inputs during the flow of a competition\.

One feature of the project that didn’t turn out as we wanted it to was the timer unit\. In designing the system, we found that the number of pins coming out of the microcontroller was not sufficient to operate all seven pins of a seven\-segment display, let alone two of them \(for the two digits\)\. As such, we decided to implement two pin extenders—one on the center console board to connect to the small, operator\-oriented display and another on the external timer unit board to connect to the large, contestant\-oriented displays—which would communicate with the microcontroller through the SDA and SCK pins via I2C \[13\]\.

This, in the end, turned out to be a mistake, as we failed to account for the bus capacitance limit of I2C communication\. As such, while the external timer unit worked, it was only capable of displaying time when connected to the center console with a one\- or two\-foot Ethernet cable; otherwise, the capacitance of the cable would exceed said limit\. This made it difficult to use the center console system in a competition setting, as there was not a practical setup where the operator would be able to control the center console and show the contestants the time left in a round without their back being shown to the audience, allowing them to view the questions and possibly even help the competitors\.

This problem was alleviated by the Bluetooth GUI option which we added later, allowing the operator to control the flow of the competition without needing to physically press the buttons, which would in turn allow them to not face their back to the audience while leaving the center console and timer unit to face the contestants, but this was still not as elegant a solution as we or our clients wanted\.

In terms of team performance, we faced significant setbacks during the course of this project, causing the system to be delayed by two weeks from when we had originally intended it to be submitted, to the point that it was only finished the day before the final regional competition \(and, as such, the organizers didn’t want to use it as they feared it could not be tested thoroughly enough in such a short amount of time\)\. Aside from the previously discussed issue of a teammate suddenly dropping the course, teamwork and spirit in the group was strong and everyone did their part well, although some mistakes in creating the PCBs \(such as accidentally having two separate ground planes or using wrong pins on the microcontroller\) caused more delays during the middle of the project\. Had there been better communication among all members from the word “go,” we could have done even more with this project, or at least completed what we have now in a much more timely fashion\.

As for where we would go next with the project, given more time, we were interested in developing both two extra button box units for the system we completed as a backup in case an overzealous contestant broke one during the course of the competition, as well as an entire second unit of the whole system so that the two installments of MATHCOUNTS we were working with could each keep one\.

We would have also liked to fix the aforementioned bus capacitance issue by replacing the I2C communication with UART, which shouldn’t have to deal with as tight a limit but would require reworking several PCBs\.

Beyond this, our clients gave us several suggestions for improvement near the tail\-end of the project, some of which we were able to implement with such a quick turnaround, such as new labels for the center console, and some of which we weren’t, such as a list of contestant buzz\-ins using the GUI option\. We would have liked to increase the functionality and versatility of the system by including these options, as they would have brought the new system closer in line with the old, making adoption that much smoother\.

# Going Further Material

Although this project will have less of an impact than other projects done for ECE senior design this quarter, which included new medical systems and military radar technology designs, that doesn’t mean it isn’t worth considering the place of the project in the world\. In an effort to move away from the corrosive idea of technological determinism—that technological developments are inevitable and that therefore it is not the responsibility of the engineers in question to seriously consider what they are creating and for who—it is important to take some time to evaluate the impact of any new technology and how it will interact with fields such as public health, the economy, and the environment\. Furthermore, as engineers, it is imperative that we look for and strive to follow codes of ethics in our practice so that if we are faced with directives to abandon these codes and create something which would be harmful or destructive, we can put our feet down and refuse to do so with confidence that those asking wouldn’t be able to find another engineer to replace us\. As such, it is also worth considering the professionally designated standards we have used as a part of our design, why they are there in place in the first place, and what it means for our project for us to have attempted to follow them\.

## I2C and RJ45 Standards

Our project uses the I2C communication protocol to allow signals to be sent from the microcontroller to the pin extenders\. This is the typical I2C standard used for ICs to communicate with each other \[15\]\.

The RJ45 standard for cable ports was also used\. This is the standard port connector used in Ethernet cables, and it allows us to guarantee that cables bought will be able to connect the center console to the button boxes and timer unit \[14\]\.

## Impacts of the Project on Broader Issues

As stated above, even for seemingly simple and inconsequential projects such as this, it is important to examine potential impacts, even if just briefly, both in terms of what could impact the project and what could be impacted by the project\. Those who would wish to exploit engineering as a profession are keen to leverage the narcissism and entitlement of those engineers who blindly and uncritically assume that nothing they develop could seriously cause any issues down that line, as well as that if they didn’t work on a risky project, someone else would have, so they might as well take the plunge and make some money while they’re at it\.

To avoid problems down the line, risks must be recognized before they are caused, so that they can at the very least be accounted for in some way\. As such, if only to form the habit, we will now analyze how our buzzer system could affect the world beyond what we intended through five separate lenses: public health, welfare, and safety; global issues; societal issues, economic issues, and environmental issues\.

### Public Health, Welfare, and Safety

As our project is designed for a competition series, albeit a very low\-stakes one, security would be a concern\. While it’s admittedly somewhat far\-fetched to believe that a student or their parent would really try to illegitimately access the Bluetooth GUI to gain some sort of advantage \(even though they wouldn’t be able to see the actual answers\), it’s still worth having some level of security, if only for peace of mind \(not to mention in order to avoid someone accidentally pairing with the system\)\. We went for a low\-level approach to security, with just a specific code needed to access the system, which should be enough to discourage any overly curious students from attempting to access the system but likely wouldn’t be enough to fully prevent anyone seriously trying to get into it\. If the system were to be developed further, to the point that it was used in more elaborate competition, this would be something that would need to be thought about further\.

Another aspect of safety we considered when developing the system was the way in which we developed the system using the idea of “poka\-yoke,” or mistake\-proofing\. While nothing in the setup of our system is very complicated \(as shown in Section 8\.1, the user’s guide\), we still wanted to make sure some more predictable mistakes were accounted for and prevented as best we could\. For example, as described in Section 2\.2\.2, the Ethernet port connecting the center console to the external timer unit carries a 9V power source, whereas the other six ports, which connect to the button boxes, only take 3\.3V\. Therefore, we not only labelled this first port to make it visually distinct from the others, we also deliberately changed the wiring order across the cable to prevent someone from accidentally frying a button box by plugging it into the wrong port\.

### Global Issues

The looming global trade war has a significant potential impact on this project in that even though few, if any, of the parts we used for this project were produced in China, many electronic components used in such devices today, particularly things like PCBs, are frequently sourced there\. Chris Miller, writing for the American Enterprise Institute \(AES\), wrote in his article “Rebalancing Trade with China Requires a More Diverse Electronics Supply Chain” that “\[o\]ver the past decade… Chinese firms have come to produce an ever\-larger share of electronic components\. Now, China can produce almost every key electronic component except advanced chips and certain specialized materials—and even in these areas, China is trying hard to catch up” \[5\]\. While it should be made known that the AEI is a fairly controversial right\-wing think\-tank, and as such may be somewhat biased in its assessment, this claim is believable as several other sources, such as the International Trade Committee, list China as by far the world’s biggest exporter of electronics, having exported over three trillion USD worth of electronics in 2023 even not counting those made in dubiously\-Chinese Hong Kong and Taiwan \[6\]\. As such, it’s reasonable to conclude that such a trade war would make it more expensive to obtain components needed to make products such as this, especially if they were to be mass\-produced, which could make it less appealing for organizations such as MATHCOUNTS, with is a nonprofit largely sustained through donations\.

### Societal Issues

We were very excited to partner with MATHCOUNTS through this project because we agree thoroughly with their mission as an organization: to “\[provide\] engaging math programs to US middle school students of all ability levels to build confidence and improve attitudes about math and problem solving” \[7\]\. MATHCOUNTS tries to not only make math exciting, through its competition series, but fun, through its series of national math clubs\. This is critically important in the US, in particular, due to our long history of anti\-intellectualism as a culture\. Larry Busk, in his article “Anti\-Intellectualism’s Not Dead: Romano, Lysaker, and American Philosophy,” in between overused grumblings about the popularity of Dan Brown books and the film God’s Not Dead, discusses the widespread influence of “the idea that intellectual life is a pernicious influence insofar as it questions \(and may thus undermine\) some sacrosanct traditional value” \[8\]\.

There’s certainly truth to this idea that the US has a history of treating higher education as “elitist” and “not worth the effort,” as evidenced by the way “I hate math” is treated as a near\-universally held opinion\. Not to mention, would a society that really valued education dismantle its own federal department in charge of overseeing such? If such a problem can be fixed, it would be through making education both more accessible and less stigmatized, so that people can both feel comfortable pursuing it and have the chance to do so in the first place\. As such, we are proud to have helped an organization like MATHCOUNTS, which is pursuing just that, even in just a small way\.

### Economic Issues

As prices get higher and higher, due to inflation, greedflation, and now tariffs, among other factors, more people have turned to making things themselves, as it can often greatly drive down overall costs, as long as the maker knows what they’re doing\. For example, if you budget things out well, it can be much cheaper to cook for yourself than eat out, to fix up clothing rather than buy replacements, or to fix your car rather than go to a shop\.

In terms of software and hardware such as our project, the discussion in this area tends to center around open\-source products\. As described by Mark A\. Lemley and Ziv Shafir in their article “Who Chooses Open\-Source Software?,”\[i\]nstead of using IP rights to optimize monetary benefit, open\-source production relies on IP rights to keep software, and any improvements or additions to it, free and widely accessible” \[9\]\.

Making the fruit of your labor free for anyone to use may seem counterintuitive in this so\-often profit\-obsessed world, but if you’re creating a product to serve a specific internal use rather than to sell to consumers, as we have with this buzzer system, it can be greatly beneficial to society to allow anyone else to work off of what you’ve accomplished\. Indeed, some of the most widely\-used services and software in the world are open\-source, such as Linux and Git\.

While our buzzer system was not designed to be open\-source, we have made an effort to properly document and save the base of the work we have accomplished, including the PCB files, the 3D models for the enclosures, and the features we were striving to accomplish through this design\. Due to this, we are confident that, if desired, a future team at Rose or elsewhere working with MATHCOUNTS could take our design as a basis to go beyond even what we accomplished\. Indeed, they could even make the system open\-source if they so choose, which would allow people from all over to use the system as a base for their own customizations\.

### Environmental Issues

A set of inherent problems in working on any electronic problem with this are the physically, culturally, and environmentally destructive ways in which the materials needed to construct necessary components are extracted\. One example of this is coltan, an ore of primarily niobium and tantalum which is essential for constructing capacitors, the lynchpin for most electrical engineering\.

According to the article “Blood on Your Mobile” by Dev Nathan and Sandip Sarkar for Economic and Political Weekly, as of 2010, not only was a significant amount of coltan mined using child labor, its sale was used to fuel an incredibly bloody and destructive civil war\. Nathan and Sarkar write that “\[o\]f the 13 major mines, 12 are controlled by armed groups, including units of the Congolese army \(FADC\) \[described earlier in the article as having been named the largest perpetrator of rape and sexual assaults on women in eastern Congo by Human Right Watch\]; DFLR \(Democratic Forces for the Liberation of Rwanda\), a Rwandan\-Hutu militia led by organizers of the 1994 Rwandan genocide; and their opponents, National Congress for the Defense of the People \(CNDP\), linked to the Rwandan regime” \[10\]\. While some initiatives to alleviate these problems to an extent, mostly though making suppliers enforce industry codes, have had some success, many, many violations of human rights still pass through what barriers have been put up \[11\]\.

While we obviously had no direct part in the destruction wrought by these mining practices, the modern world of globalization and technological dependence makes it nearly impossible to fully escape from having a part in it, especially when working in electronic hardware\. For example, some of the devices most often implicated in these kinds of investigations into unethical mining are smartphones, which have essentially become necessary to participate in modern society\. Ultimately, while it may not be worth dwelling too much on these systemic forces which we have next to no control over, it is still worth considering them at least in some part of the mind when making design decisions, especially if any of us end up in a position where we would be making these kinds of sourcing decisions ourselves\.

## System Failure Mode Analysis

Our system is generally simple enough to not have many modes of failure\. However, there are still a few, with one key example relating to the I2C connection to the timer unit\. Sometimes, in the operation of the system and especially when the Ethernet cable connecting the timer unit to the center console is longer than one or two feet, the signal between the two gets lost, likely due to bus capacitance\. Another potential mode of failure would be the system accidentally getting unplugged which would, of course, turn the system off until it is plugged back in\.

For the risk priority number calculations of the I2C failure, the probability is a 4/5, since with the current design this is going to happen at some point but not super frequently\. The severity is a 2/5 because the system still works and resetting it fixes the error\. The detection gets a 2/5 because it is obvious when it happens, but it shows no signs of potential failure before it happens\.

In the calculation for unplugging the system, the probability is a 2/5 because it can happen but rarely, especially if the user is careful\. The severity is a 1/5 because if you plug it back in then nothing changes\. The detection is a 1/5 because it is very noticeable and the user can tell if the way they’re using the system will likely result in it being unplugged\.

As determined through these calculations, shown in Table 10, below, the system accidentally being unplugged is a very low risk that does not need any design decisions, so the job of leaving the system plugged in goes to the user\. The I2C failure, however, is a moderate risk mode of failure\. Therefore, from a design perspective, the timer unit should be using a different communication standard than I2C\.

System failure mode analysis

||Probability|Severity|Detection|RPN|
|---|---|---|---|---|
|I2C Failure|4|2|2|16|
|Unplugged|2|1|1|2|

# Annotated Bibliography

\[1\] E\. Herrada, “DIY Quiz Show Controller System,” Adafruit Learning System, Jun\. 22, 2021\. https://learn\.adafruit\.com/quiz\-show\-controller\-keyboard\-bluetooth \(accessed Sep\. 16, 2024\)\.

This article gave us ideas on how to design the general architecture of the system\.

\[2\] P\. Prinyak, “SLA Resin Strength: How Strong Is 3D Printed Resin?,” All3DP, Aug\. 23, 2024\. https://all3dp\.com/2/sla\-3d\-printing\-how\-strong\-is\-resin/

This article helped determine the viability of 3D printing the enclosure\.

\[3\] “Plywood vs Solid Wood: 5 Key Differences in Detail,” Cameroon Wood Export, Aug\. 12, 2022\. https://cameroontimberexport\.com/plywood\-vs\-solid\-wood/

This article helped determine the viability of building the enclosure out of wood\.

\[4\] “What is SBR Rubber?, Custom Rubber Corp,  https://www\.customrubbercorp\.com/services/rubber\_materials\_used/sbr\-rubber

This article helped us decide that we should pad the units with rubber to improve impact resistance\.

\[5\] Miller, Chris\. Rebalancing Trade with China Requires a More Diverse Electronics Supply Chain\. American Enterprise Institute, 2025\. JSTOR, http://www\.jstor\.org/stable/resrep66305\. Accessed 30 Mar\. 2025\.

This article is a criticism and analysis of China’s microchip manufacturing policies\. It should be noted that this was written for a very controversial right\-wing thing tank, and as such it should not be too surprising that much of the article is very nationalistic and fear mongering\-adjacent, making it less useful as an overview of trade policies\. Still, it was used as a sort of basis for our analysis so it is included here\.

\[6\] \(ITC\), International Trade Centre\. “Trade Statistics for International Business Development\.” Trade Map \- List of Exporters for the Selected Product in 2023 \(Electrical Machinery and Equipment and Parts Thereof; Sound Recorders and Reproducers, Television \.\.\.\)\. Accessed 30 Mar\. 2025\.

This is a ranked list of exporters of electronic components in 2023 in terms of categories like gross profit from such trade\.

\[7\] OUR STORY \| MATHCOUNTS Foundation\. \(2023\)\. Mathcounts\.org\. https://mathcounts\.org/about/our\-story

The background about the MATHCOUNTS competition and tutoring series given by the foundation itself\. This was used to examine the mission statement of MATHCOUNTS\.

\[8\] Larry Busk\. “Anti\-Intellectualism’s Not Dead: Romano, Lysaker, and American Philosophy\.” The Pluralist, vol\. 11, no\. 2, 2016, pp\. 49–63\. JSTOR, https://doi\.org/10\.5406/pluralist\.11\.2\.0049\. Accessed 31 Mar\. 2025\.

This is an opinion piece on the state of anti\-intellectualism in the country\. Much of the article was diatribes about the popularity of franchises such as The Hunger Games, which were both tiresome and not relevant to this report, but there were some tidbits about anti\-intellectualism's state in the country which were used\.

\[9\] Lemley, Mark A\., and Ziv Shafir\. “Who Chooses Open\-Source Software?” The University of Chicago Law Review, vol\. 78, no\. 1, 2011, pp\. 139–64\. JSTOR, http://www\.jstor\.org/stable/41552853\. Accessed 3 Apr\. 2025\.

This is an analysis of what open\-source software is, some examples of it, and why people are attracted to its usage\.

\[10\] NATHAN, DEV, and SANDIP SARKAR\. “Blood on Your Mobile?” Economic and Political Weekly, vol\. 45, no\. 43, 2010, pp\. 22–24\. JSTOR, http://www\.jstor\.org/stable/20787494\. Accessed 3 Apr\. 2025\.

This is a description the mining practices used to extract coltan, a critical component in capacitors, as well as how said practices have affected child labor and violence in the DRC, where something in the range of 10% of it is mined\.

\[11\] Diemel, J\.A\. “Authority and access to the Cassiterite and coltan trade in Bukama territory \(DRC\)\.” The Extractive Industries and Society, vol\. 5, no\. 1, 6 Dec\. 2017, pp\. 56–65, https://doi\.org/10\.1016/j\.exis\.2017\.12\.001\.

A more modern overview of the coltan trade in the DRC, as well as some analysis about what has been done to fix the problems described in the pervious source\.

\[12\] BuzzerSystems\.com, “Quiz Bowl and game show Buzzer Systems \| Buzzer System,” BuzzerSystems, Mar\. 28, 2025\. https://buzzersystems\.com/

A sampling of commercially available buzzer systems we used to compare the price of our final product\.

\[13\] Arduino, “Nano 33 IoT,” Arduino Docs\. https://docs\.arduino\.cc/hardware/nano\-33\-iot/ \(accessed May 09, 2025\)\.

The selling and technical information for the microcontroller we used in our design\.

\[14\] Digikey and J\. Smoot, “RJ45 Connectors – What You Need to Know,” Jun\. 02, 2022\. https://www\.digikey\.com/en/articles/rj45\-connectors\-what\-you\-need\-to\-know?msockid=1fbb8d87cc416a5933399fadcdd26b64 \(accessed May 09, 2025\)\.

Information on the RJ45 standard we used in this project\.

\[15\] NXP Semiconductors, C\-bus specification and user manual\. \[Online\]\. Available: https://www\.nxp\.com/docs/en/user\-guide/UM10204\.pdf

Information on standards and usage for the I2C protocol\.

# Appendices

## Appendix A: User Guide

MATHCOUNTS Buzzer System Operational Guide

Center Console Layout:

Center Console Top View

Setup:

Plug the supplied 9V power cord into the power jack on the side of the center console\.

Plug one end of the short \(1ft\) Ethernet cord into the Ethernet port marked with a symbol over it \(the leftmost port when facing the Ethernet ports\)\. Plug the other end into the External timer unit and face it towards the contestants\.

Plug 2\-6 of the long \(25ft\) Ethernet cables into any of the other Ethernet ports, according to the number of contestants\. Plug the other ends into button boxes\.

Flip the Power Switch to the “on” position\.

Basic Operation:

When the system is connected to power, it will begin with the timer stopped\.

While in this state, you can change the length of time for which the timer will run by pressing the Up and Down buttons\. These will respectively increase and decrease the length by five seconds, with a minimum length of 20 seconds and a maximum of 60 seconds\.

When the timer is stopped, the button boxes \(with which the contestants will buzz in\) will be inactive\.

To activate them and begin the countdown, press the Start button\. To return to this inactive state at any time, press the Reset button\. Pressing the Start button while the timer is counting down will have no effect\.

Pressing the Start button will cause the timer to tick down from its initial time until it reaches 0, at which it will stay until Reset is pressed again\. This will cause the button boxes to again become inactive\. Additionally, reaching zero will cause the system to play one of three sounds, to indicate the end of the contestant’s time for that question\.

Up and Down will be inactive when the timer is either counting down or stopped at zero\. They will only be active when the timer is reset to its initial time\.

While the timer is running, the first contestant to answer the question will buzz in by pressing the button on their box\. This will cause the second of three noises to play from the system\. This will also cause the bulb to light up on their box\. While any contestant’s bulb is lit up, no other contestants’ button presses will cause a sound to play or a bulb to light up, but the presses will still be recorded by the system\.

Mode\-Dependent Operation:

There are two modes the system can operate in: Type 1 mode and Type 2 mode\. This is changed by toggling the Mode Switch and is checked when the Start button is pressed to begin the countdown\. In other words, the mode the system will operate in while the system is running is dependent on what position the Mode Switch was in when the countdown began—toggling it while mid\-round will have no effect\.

In Type 1 mode, pressing the Next button will cause all bulbs to turn off, but will still not allow contestants who have buzzed in for that question to buzz in again\.

In Type 2 mode, the system will keep a queue of all contestants who buzzed in while another contestant was answering a question \(indicated by the bulb on their button box being lit up\)\. A contestant can only be added to this queue once—no contestant can buzz in multiple times for one question\.

While in Type 2 mode, if the queue is empty and the Next button is pressed, the system will turn all bulbs off and contestants who have not yet buzzed in that round will be given a chance to answer before the timer reaches zero\.

Alternatively, if another contestant’s button press was recorded in the queue, pressing the Next button will turn off the bulb that is currently on and turn on that of the next contestant to have buzzed in for that question, playing the third of three sounds from the system, both of which will indicate that said contestant now has the floor to answer\. Pressing Next Answer yet again will continue for the next contestant in the queue in the same fashion\.

Pressing Next in Type 2 mode when all contestants have already buzzed in for the round will cause all bulbs to turn off while still counting down the timer and not allow any more buzz\-ins\. It is advisable to press the Reset button at this point\. Additionally, pressing Next when no contestants are currently answering the question \(in other words, no bulbs are on\) will have no effect\.

List of Bluetooth Commands:

“start”: functions the same as Start\.

“reset”: functions the same as Reset\.

“next”: functions the same as Next\.

“timerup”: functions the same as Timer Up\.

“timerdown”: functions the same as Timer Down\.

“timer \#\#”: sets the initial timer value to \#\#\. Must be between 10 and 95\.

“buzzfreq \#\#\#”: sets the frequency of the sound that plays when a contestant buzzes in to \#\#\#\.

“nextfreq \#\#\#”: sets the frequency of the sound that plays when Next is pressed in Type 2 mode to \#\#\#\.

“timeoutfreq \#\#\#”: sets the frequency of the sound that plays when the timer reaches 0 to \#\#\#\.

“tune \#\#\#\#”: sets the timer to tick down every \#\#\#\# milliseconds\. Used to tune around arduino clock’s inaccuracy\.

## Appendix B: Bluetooth Commands, Explained

The Bluetooth GUI is set up to mirror the button interface with a few extra commands\. The user is presented with a button labeled “connect\.” Pressing this button will attempt to connect to the system until a connection is made\. Below that, there are 5 buttons tha are identical in function and name to the 5 buttons on the center console\. At the bottom, there is a command line where any command can be typed and transmitted\. For the purposes of the next few sentences, all variable entries will be listed in bracket\. “start,” “reset,” “next,” “timerup,” and “timerdown” all have the same function as the same function as the buttons\. “timer \[k\]” will set the timer to k if it is between 10 and 95\. “buzzfreq \[k\]” will set the frequency of the sound that plays when a contestant buzzes in to k if it is between 100 and 999\. “nextfreq \[k\]” will do the same for the frequency of the sound that plays when next is pressed in Type 2 mode to switch to a queued answer, and “timeoutfreq \[k\]” will do the same to the frequency of the sound that plays when the timer reaches 0\. Finally “tune \[k\]” will set the timer to tick down every k milliseconds if it is between 100 and 9999\. If the Arduino clock begins to drift, this command can be used to adjust the rate the timer ticks down\.

> （注：部分内容由豆包工作 AI 生成）
