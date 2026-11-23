# 测试方案与测试报告（Test Plan and Report）

New MATHCOUNTS Buzzer System

Test Plan

Created By:

Calvin Hamilton

Bhargav Nagalamadaka

Jacob Rast

Linus Wise

Clients:

Dr\. Leanne Holder

Jim Reppell

Date:

12/9/24

# 1\.      Background

This test plan describes the verification for requirements B\-2, F\-5, and F\-6\. These tests are involved with the functionality of the next answer\-button control of the center console\. This behavior is one of the primary motivations behind this project since the clients wanted to be able to vary the system behavior based on the competition format\. The control should be able to poll the mode switch, and then accurately change the button behavior based on the result\. Furthermore, all center console controls should show results at the contestant button boxes in a timely manner; using the next answer button we can use an oscilloscope to show that the time from the center controller receiving an entry to the button boxes registering an output is less than 20ms\.

To verify the behavior for this system control system we will use a two\-part test\. The first test will verify that the system response time is fast enough \(less than 20ms\) using an oscilloscope on multiple contestant units\. The second test will verify that the next answer system behaves properly in either of the two system modes, and with a varied number of contestant boxes in play\. The test of the system modes will require no errors in behavior when buttons are pushed and will ensure that the system is completely reliable and functional to the system designs\.

The first part of this test is to verify requirement B\-2: that signals originating in the center console can affect the LEDs on the button boxes within 20ms\. This will be accomplished by first hooking up an oscilloscope to the test vias in the signal line of the next answer button on the center console PCB and the LED input line on the button box PCB\. After ensuring that the system is in the “timer running” mode and that the button on said button box has been pressed \(i\.e\., its LED should be on\), we will press the next answer button\. Regardless of the current mode of operation, with no other button box buttons having been pressed at this point, pressing the next answer button should cause this LED to turn off\. Therefore, we will measure the time between the press of the next answer button and the LED signal going off\. To pass this requirement, we should be confident that the difference between these two slopes is less than 20ms\.

The second part of the test is to verify requirements F\-5 and F\-6: that the two different modes of the next answer functionality operate as intended\. To accomplish this, we will hook up six button boxes to the center console\. When the mode switch is flipped to Type 1, we will test pressing next answer with each possible ordering of button box LEDs lit up \(including none\)\. There are seven possible orderings, so we will test each twice with all six boxes plugged in, then once with only that box plugged in \(still in the same port\), in order to verify that this works with different combinations of button boxes plugged in\. Similarly, we will test the functionality of the Type 2 mode by starting with all six button boxes plugged in\. We will test the functionality of each box’s LED being on, then switching to another button; each box’s LED being turned on by being next in the queue when another box’s LED is currently on \(this will be tested twice, with two different originally\-on LEDs\); and each box’s LED being turned off with no other LEDs turning on \(due to no other “contestant” having buzzed in\)\. Following this, we will test the functionality of this button with each button box being the only once plugged in, wherein it should function as it does under the Type 1 mode, and one time with no button boxes plugged in, just to make sure that nothing breaks when this happens\.

# 2\.      Requirements to be Verified

|Feature|ID|Requirement|Validation|
|---|---|---|---|
|Button Boxes, Center Console|B\-2|Signals originating from center console buttons and going to the button box will register in under 20ms|Signals coming from center console to the button box aren’t as urgent as those going the other way, since they aren’t inherently timing\-dependent like buzzing\-in is but still need to be reflected in the LEDs quickly enough that   the competition can run frictionlessly\.|
|Functional, Center Console|F\-5|30 out of 30 times, the Next Answer button will, while in Type 2 mode, turn off the current contestant’s LED and turn on that of the next contestant, while playing the secondary buzz\-in sound\. If no more contestants had buzzed in, this button will act as it does in the Type 1 mode and turn off all LEDs while re\-enabling buzz\-ins\.|This is needed to ensure the functionality of the Type 2 mode\.|
|Functional, Center Console|F\-6|21 out of 21 times, the Next Answer button will turn all LEDs off and allow for new buzz\-ins when in Type 1 mode\.|This is needed to ensure the functionality of the Type 1 mode\.|

# Equipment and Procedure

## 3\.1  Test Part 1 Details

Test Objective: B\-2

### 3\.1\.1 Equipment

- Oscilloscope \(Tektronix MDO4014B 100MHz\)

- MATHCOUNTS center console unit \(with unedited base software installed\)

- 9V/18W power supply with barrel plug \(included with unit\)

- 2x MATHCOUNTS button box unit

- 2x 25 ft ethernet cable

- Flash Drive \(at least 1GB\)

### 3\.1\.2 Procedure

- Begin with the center console unit, and button boxes removed from their 3d printed housing with the power switch turned off\.

- Connect probe 1 of the oscilloscope to the Next Answer line on the breadboard version of the center controller\.

- Connect probe 2 and 3 to the LED on the breadboard for button box 1 and 2\.

- Set a falling edge trigger on the oscilloscope at 2V for probe 2\.

- Plug the 18W supply into the center console and use both ethernet cables to plug in the button boxes to the center console\. Turn the power switch on, ensure the built\-in timer display powers on\.

- Press the “Timer\-Up” button until the center console displays 60\.

- Press the “Start” button on the center console, the timer should begin to tick downwards\.

- With the timer ticking down, press the contestant input button on button box 1, then press the same button on button box 2\. Probes 1 and 2 should now read a high voltage of 3\.3V and probe 3 should be low\.

- Then press the next answer button on the center control unit, probe 1 should trigger\.

- On the oscilloscope, add a marker to where the falling edge of probe 1 reached 2\.7V the first time\. Add another marker to where the rising edge of probe 4 reached 2\.7V\. Add a delta time measurement for the difference between both markers\. Record the screen of the oscilloscope and save the image to the flash drive\. If the time difference is less than 20ms, this test will pass\.

- Press the reset button on the center controller\.

- Repeat steps 7\-11, this time press button box 2 first and record the difference between the falling edge of probe 1 and the rising edge of probe 2\. In step 8, probes 1 and 3 should read a high voltage, and probe 2 should be low\.

- Repeat steps 7\-12 2 times, only record the delta time measurement\.

## 3\.2 Test Part 2 Details

Test Objectives: F\-5 and F\-6\.

### 3\.2\.1 Equipment

- MATHCOUNTS team center console unit

- 9V/18W power supply with barrel plug \(included with unit\)

- 6x MATHCOUNTS button box unit

- 6x 25 ft ethernet cable

### 3\.2\.2 Procedure

Testing Type 1 Mode:

Begin with the center console unit and button boxes in their 3d printed housing with power switch flipped off\.

Plug 9V barrel plug into power jack and flip power switch to “on” position\.

Plug all six button box units into separate ethernet cables, then connect each cable end to a separate port of the center console \(NOTE: don’t plug into the port closest to the power jack: that is for the timer unit and cannot be used for a contestant box\.\)

Verify that the mode switch is in the “Type 1” position\.

Press the reset button, then the start button to begin operation\.

Within 20 seconds of pressing start, press the button on the first button box such that the LED lights up, then press the next answer button\. Press the next answer button and verify that the LED of the first button box turns off\. If It did, record a pass, if not record a fail\.

Press the reset button, then the start button and repeat step 6 for each button box two times each\.

Unplug all but one ethernet cable from the center console and repeat steps 5 and 6\.

Repeat step 8 for each button box two times, making sure that each ethernet cable is plugged into a separate port\.

Testing Type 2 Mode:

Connect each unplugged ethernet cable end to a separate port of the center console \(NOTE: don’t plug into the port closest to the power jack: that is for the timer unit\.\)

Flip the mode switch to the “Type 2” position\.

Press the reset button, then the start button to begin operation\.

Within 20 seconds of pressing start, press the button on the first button box such that the LED lights up, then press the button on any other button box\. Next, press the next answer button and verify whether or not the LED on the first button box turns off\. Record this result\.

Press the reset button, then the start button and repeat steps 12 and 13 with each button box being first to have its LED lit up once\.

Press the reset button, then the start button and, within 20 seconds, press the button of any button box but the first, then press the button on the first button box\. The LED of the first button box should be off and the LED of the other button box whose button you pressed should be on at this point\. Press the next answer button and verify that the LED of the first button box turns on\. If It did, record a pass, if not record a fail\.

Repeat step 15 with each button box being second to have its LED lit up twice\.

Repeat steps 8 and 9 with the mode switch still flipped to “type 2\.”

Unplug the final ethernet cable\. Press the reset button, then the start button, and then the next answer\. Verify if anything notable happens after doing this and record this result\.

# Data Collection, Analyses, and Conclusion

## 4\.1 Test Part 1

\(Dark blue\) Next Answer button is pressed

\(Teal\) LED turns off

Figure 1 – This image shows the first timing difference test for contestant button box 1 between the first press of the next answer button and when the system response by disabling the light of the contestant with an LED that is high\. A successful time shows a delta t measurement of less than 20 ms\. This time is further recorded in table 1\.

\(Dark blue\) Next Answer button pressed

\(Pink\) LED turns off

Figure 2 – This image shows the second timing test for the next answer button\. This test differs from test one only in that a different button box is now being used, this is done to make sure that the same timing and behavior is observed with multiple different contestant button units\. This test should also show a delta t measurement of less than 20 ms, and is recorded in the second column of trial 1 in table 1\.

Test Part 1, all timing results

||Contestant 1 first time delay \(ms\)|Contestant 2 first time delay \(ms\)|
|---|---|---|
|Trial 1|11ms|11ms|
|Trial 2|11ms|11ms|
|Trial 3|11ms|11ms|

Table 1 – This table contains all delta t measurement from the scope testing of the next answer buttons, all measurements should be less than 20 ms to pass

Requirements to Pass Part 1:

In order to verify requirement B\-2, every measurement in table 1 should show a timing difference of less than 20ms\. The data in Table 1, and Figures 1 and 2 is relevant to B\-2, and show the system response time to system control commands input via control buttons\.

Conclusion for Part 1:

As can be seen in Figures 1 and 2, the delay is, as we aimed for, below 20ms in every test\. As is seen in Table 1, this is a consistent result for all button boxes, ensuring operation of this system should be smooth and reliable\.  The delay in all trials was 11 ms, this means we passed requirement B\-2 with a margin of 9ms\. This margin could be increased if necessary since we presently have a 10ms count in the code on press as a button debounce measure, however, as is it is more than satisfactory for this system requirement\.

## 4\.2 Test Part 2

|Test|Pass/Fail|Test|Pass/Fail|
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

Table 2: Part 2 Type 1 Results

Table 2 shows the results from testing the next answer button while in type 1 mode\. Tests 1\-12 are tests when all 6 contestant buttons are being used, tests 13\-24 show results when only a single contestant box is plugged in\. To verify requirement F\-6, every test should pass\.

|Test|Pass/Fail|Test|Pass/Fail|Test|Pass/Fail|
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

Table 3: Part 2 Type 2 Results

Table 3 shows the results from next answer button testing while in type 2 mode\. Tests 1\-6 are tests for cases where a second contestant has not answered, and the next answer button has been pressed\. Tests 7\-18 test that every contestant can be cycled to if there was already an active contestant LED\. Tests 19\-30 verify that the next answer button will still behave as expected if only a single contestant box is plugged in\. To verify requirement F\-5, every test should pass\.

Requirements to Pass Part 2:

To pass this test, we expect a 100% success rate for all tests\. In other words, we will not be satisfied with this requirement’s performance unless every iteration of the test results in a “pass\.”  A “pass” in this experiment is given when a firm button press which fully compresses the button until it cannot be pushed further results in an expected LED state change as described in the procedure\. If a button press does not cause the expected LED change, the test is recorded as a “fail”\.

Conclusion for Part 2:

The next answer functionality worked perfectly in both modes for every trial, this verifies that the next answer button will work in either mode, and with varied button boxes plugged in to the center controller\. The margin of error for this experiment was 0 due to the 100% pass requirement, as every test was a success, F\-5 and F\-6 have passed\. We can be confident that this feature will work reliably in the competition regardless of the number of participants\.

> （注：部分内容由豆包工作 AI 生成）
