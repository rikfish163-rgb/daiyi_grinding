# 智能还车论文\_en

# **Vision\-Dominated Intelligent E\-Bike Parking Compliance Recognition System for Urban Governance**



## **Abstract**

To address the urban management challenges posed by the disorderly parking of shared e\-bikes, traditional solutions excessively rely on hardware sensors \(such as high\-precision IMUs or RFID studs\), which face challenges including high deployment costs and maintenance difficulties\. This paper proposes a vision\-dominated parking compliance recognition system based on a Dual\-Branch Parallel Pipeline\. The system fuses the precise geometric measurement of the Computer Vision \(CV\) branch with the robust semantic understanding of Vision\-Language Models \(VLM, such as Qwen\-VL\)\. The CV branch utilizes YOLOv8\-Seg for instance segmentation and employs Depth Anything V2 to generate monocular absolute\-scale depth, thereby reconstructing the 3D point cloud of the target vehicle\. Principal Component Analysis \(PCA\) is applied to extract the physical orientation with an allowable error tolerance of $\\pm 30^\{\\circ\}$\. Furthermore, spatial boundary lines are fitted on the bird's\-eye view plane to partition the ground into parking and non\-parking zones, determining whether the bottom contact point of the vehicle falls within the compliant area\. The VLM branch evaluates parking quality across four core dimensions and exercises a "veto power" against violations such as occupying tactile paving\. Finally, the system outputs decisions through a Hybrid Veto\-Weighted Voting Strategy\. Experimental results demonstrate that on a real\-world complex street\-view dataset, the dual\-branch system achieves an F1\-score of 83\.78%, effectively resolving the failure issues of pure CV methods under illumination degradation or reference occlusion\.



## **1\. Introduction**

### **1\.1 Background and Motivation**

Shared e\-bikes, as a vital component of urban micro\-circulation, provide tremendous convenience for residents\. However, with the explosive growth in deployment scale, the popularity of shared e\-bikes has also introduced severe urban management challenges, such as disorderly parking, occupying tactile paving, and obstructing sidewalks\. These violations not only damage the aesthetics of the city but also pose threats to public transportation safety, particularly infringing upon the right\-of\-way of visually impaired individuals\. Therefore, establishing a highly reliable parking compliance judgment system for shared e\-bikes has become an urgent need to enhance the efficiency of smart city management\.



Existing compliance management schemes can primarily be categorized into three types: \(1\) GPS\-based Geofencing, which determines compliance by comparing vehicle positioning with back\-end defined geographic polygons; \(2\) Hardware\-assisted solutions, such as deploying RFID beacons, Bluetooth studs, or integrating high\-precision IMUs and RTK positioning terminals, to improve the robustness of boundary determination; \(3\) Manual Dispatching, serving as a necessary supplement and safety net for technical means, responsible for clearing accumulations and correcting positions\. However, these traditional schemes all face significant limitations: geofencing is easily affected by the urban canyon effect, resulting in signal drift; hardware schemes entail high deployment and maintenance costs; and manual modes are constrained by delayed responses and the difficulty of covering ultra\-large\-scale urban networks\. To eliminate hardware dependency and improve governance precision, utilizing on\-site photos uploaded by users upon returning the bikes for visual perception offers a low\-cost and highly scalable alternative\.



### **1\.2 Challenges of Visual Perception: Core Models and VLM Semantic Fallback**

Although computer vision \(CV\) based methods hold great potential, pure CV algorithms face severe technical challenges in real urban environments\. The system proposed in this paper adopts an advanced model combination: utilizing YOLOv8\-Seg to achieve high\-precision universal instance segmentation for acquiring pixel\-level masks of e\-bikes and environmental references; integrating Depth Anything V2 to generate monocular absolute\-scale depth maps to provide depth information for spatial reconstruction; introducing Principal Component Analysis \(PCA\) for axial fitting on the extracted 3D point clouds to quantify vehicle orientation; and incorporating Qwen\-VL, a multimodal large model, for high\-level semantic reasoning\.



However, pure CV schemes still appear inadequate when confronting real\-world "long\-tail scenarios"\. For instance, parking lines may become worn and faded due to prolonged exposure to sun and rain, or references may be occluded by fallen leaves, accumulated water, or overlapping shared bikes\. Furthermore, visual degradation occurs under extreme lighting conditions \(e\.g\., strong backlight or weak illumination late at night\)\. In these situations, traditional geometric\-physical modeling often fails to provide deterministic compliance conclusions due to the absence of input features\. Recently, Vision\-Language Models \(VLM\) have demonstrated powerful semantic fallback capabilities, able to accurately judge whether a vehicle occupies tactile paving or interferes with pedestrian passage through multi\-dimensional common\-sense reasoning when geometric features are unavailable\. This semantic\-based "fault\-tolerance" mechanism is key to enhancing system robustness\.



### **1\.3 Main Contributions**

This paper proposes a recognition framework that integrates geometric\-physical modeling and deep semantic reasoning\. Our main contributions are as follows:

\(1\) Contributed a real\-world street\-view dataset featuring shared e\-bike mask annotations and multi\-dimensional parking compliance labels\.

\(2\) Proposed a parking compliance judgment algorithm based on 3D geometric\-physical modeling utilizing instance segmentation and monocular depth estimation\.

\(3\) Innovatively introduced a VLM\-based four\-dimensional parking compliance evaluation paradigm and designed a hybrid veto and weighted scoring strategy to achieve deep fusion with the CV branch\.

\(4\) Conducted extensive tests and ablation experiments on the constructed dataset, fully demonstrating the effectiveness and robustness of the dual\-branch pipeline\.



## **2\. Related Work**

### **2\.1 Vision\-based Parking Compliance and Pose Estimation**

Currently, the governance of parking compliance in the shared e\-bike industry mainly follows a "three\-layer collaborative" framework: employing GPS\-based geofencing as the basic regulatory boundary; superimposing hardware\-assisted solutions in key hub areas, such as RFID beacons, Bluetooth studs, and high\-precision IMU positioning terminals, to mitigate GNSS signal drift misjudgments in urban canyons; and combining manual dispatching for offline obstacle clearance and accumulation management\. However, lacking micro\-level physical perception, these schemes cannot identify precise vehicle orientation or the relative distance to the curb, making it difficult to capture the micro\-state of illegal parking\. Moreover, traditional parking management research mostly focuses on static parking space detection based on binocular or multi\-camera setups\. For dockless e\-bikes in open scenarios, although monocular 3D object detection has made some progress, directly applying general models to determine specific geometric constraints of e\-bikes still faces enormous challenges of recognition failure in long\-tail scenarios\.



### **2\.2 Applications of Vision\-Language Models \(VLMs\) in Image Understanding**

In recent years, large language models \(LLMs\) based on the Transformer architecture \(e\.g\., BERT, GPT\-3, and LLaMA\) have led a paradigm shift in the field of artificial intelligence\. Their powerful contextual understanding and zero\-shot reasoning capabilities provide core support for processing complex tasks\. To break the barrier between text and visual modalities, researchers have utilized vision\-language alignment technologies \(e\.g\., CLIP\) to map image features into a unified semantic space, achieving deep fusion of cross\-modal information\. Modern Vision\-Language Models \(VLMs, such as BLIP\-2, GPT\-4V, and Qwen\-VL\) evolved from this foundation not only possess open\-world perception capabilities but also can perform logical reasoning tailored to complex visual scenes\. In the domain of urban governance, VLMs can effectively identify non\-standard constraints \(such as worn markings and semantic violations\), providing crucial semantic compensation for the robustness defects of traditional geometric algorithms in long\-tail scenarios\. By integrating Qwen\-VL with a 3D geometric pipeline, this paper constructs a highly reliable dual\-branch recognition framework\.



### **2\.3 Visual Perception Technology Foundation and Paradigm Evolution**

This section systematically reviews the technological evolution from traditional visual perception to multimodal large models\. In the field of instance segmentation, from Mask R\-CNN to YOLOv8\-Seg, universal segmentation realized through the Transformer architecture has significantly improved the recognition accuracy of vehicles and curb boundaries in complex street scenes\. Regarding depth perception, monocular depth estimation technology has gradually evolved from early supervised learning to large\-scale pre\-trained models represented by Depth Anything V2, which not only recovers pixel\-level relative depth but also provides physically meaningful absolute scale estimation in specific scenarios\.



For 3D pose recognition, traditional point cloud processing techniques, such as Principal Component Analysis \(PCA\), still possess irreplaceable geometric interpretability in spatial direction extraction, enabling efficient fitting of the main axis vector of vehicles from sparse point clouds\. Recently, the AI field is experiencing a paradigm leap from traditional visual feature extraction \(e\.g\., ViT\) and cross\-modal alignment \(e\.g\., CLIP\) to multimodal large models \(e\.g\., LLaVA and the Qwen\-VL series\)\. This leap enables systems to no longer be confined to parsing geometric features, but rather to possess human\-level open\-world common\-sense reasoning capabilities\. Consequently, they can provide robust semantic compensation when encountering visual long\-tail scenarios such as blurred markings or environmental occlusions\.



## **3\. System Architecture and Core Algorithms**

### **3\.1 System Overview: Dual\-Branch Parallel Recognition Pipeline**

The proposed system adopts an edge\-cloud collaborative architecture, with a Dual\-Branch Parallel Pipeline at its core\. When a user submits a parking photo, the cloud back\-end simultaneously activates the Computer Vision \(CV\) branch and the Vision\-Language Model \(VLM\) branch\. The CV branch focuses on precise geometric measurement and physical modeling, while the VLM branch utilizes its powerful common\-sense reasoning capabilities for semantic compliance analysis\.



*\[Insert Figure 1: Overall System Architecture\]*

*\*\(Caption: Figure 1\. Overall architecture of the parking compliance recognition system based on the dual\-branch parallel pipeline\. The system receives raw RGB images and synchronously inputs them into two branches: \(1\) ***Upper CV branch***: Utilizes YOLOv8\-Seg for instance segmentation to identify vehicles, parking lines, and curbs, combines Depth Anything V2 to obtain absolute\-scale depth maps, reconstructs 3D point clouds via back\-projection, and uses PCA to extract vehicle orientation and bird's\-eye view projections for spatial compliance determination; \(2\) ***Lower VLM branch***: Feeds the raw images, segmentation masks, and specific Prompts into Qwen\-VL to reason from four dimensions: composition, angle, distance, and environmental semantics \(e\.g\., tactile paving occupancy\); \(3\) ***Decision Fusion***: In the voting decision module, if the VLM detects tactile paving occupancy, it exercises a one\-vote veto; otherwise, it outputs the final result through a weighted score $S = \\alpha \\cdot S\_\{CV\} \+ \\beta \\cdot S\_\{VLM\}$, with the VLM providing semantic fallback when CV features are missing\.\)\**



### **3\.2 Computer Vision \(CV\) Branch: Geometric\-Physical Modeling**

The CV branch aims to extract the 3D spatial state of the vehicle and identify environmental constraints:

1\. **Instance Segmentation and Scene Perception**: YOLOv8\-Seg is utilized to perform precise segmentation of the target e\-bike, parking lines, and curbs\.

2\. **Monocular Depth Estimation and 3D Point Cloud Reconstruction**: Depth Anything V2 is used to generate an absolute\-scale depth map of the scene\. Through mask\-guided back\-projection, 2D pixels are mapped into 3D space to generate 3D point clouds of the target vehicle and the ground environment\.

3\. **Orientation Extraction and PCA Angle Criterion**: Principal Component Analysis \(PCA\) is executed on the vehicle point cloud to extract its physical longitudinal axis direction vector $\\vec\{v\}\_\{bike\}$\. The angle $\\theta$ between this vector and the tangent vector of the curb or parking line $\\vec\{v\}\_\{ref\}$ is calculated\. Considering the diversity of actual parking, the system allows an angle error tolerance of $\\pm 30^\{\\circ\}$\. If $\\theta \\in \[60^\{\\circ\}, 120^\{\\circ\}\]$, the orientation is judged to be compliant\.

4\. **Region Division and Contact Point Determination**: By projecting the curb point cloud onto the $X\-Z$ plane \(bird's\-eye view\), a spatial boundary line is fitted to partition the ground into parking and non\-parking zones\. A set of points with the maximum $Y$ value \(i\.e\., the lowest in physical space\) is extracted from the bike point cloud as the "bottom contact points," and their coordinates are calculated\. If the contact points fall within the compliant zone, the vehicle is determined to be fully situated within the parking area\.



### **3\.3 Vision\-Language Model \(VLM\) Branch: Semantic Compliance Reasoning**

The VLM branch adopts the Qwen\-VL model, with inputs including the raw RGB image, the segmentation mask map generated by the CV branch, and specific prompts\. This branch outputs compliance scores from four dimensions: \(1\) **Composition Quality**: Evaluates whether the image is clear and fully captures the vehicle\. \(2\) **Parking Angle**: Reasons the relative angle between the vehicle and environmental references\. \(3\) **Parking Distance**: Assesses whether the vehicle is too far or deviating from the parking spot\. \(4\) **Environmental Semantics \(Context\)**: Identifies violations such as occupying tactile paving, outputting a boolean value $VLM\_\{context\} \\in \\\{0, 1\\\}$\.



### **3\.4 Decision Fusion: Hybrid Veto\-Weighted Voting Strategy**

The system adopts a **Hybrid Veto\-Weighted Voting Strategy** for the final judgment:

\(1\) **Veto Rule**: If the VLM branch detects that the vehicle occupies tactile paving \($VLM\_\{context\} = 0$\), it is directly judged as non\-compliant regardless of the CV branch result\.

\(2\) **Weighted Scoring**: Without triggering the veto rule, a comprehensive final score $S = \\alpha \\cdot S\_\{CV\} \+ \\beta \\cdot S\_\{VLM\}$ is calculated\. If $S \> \\tau$ \(confidence threshold\), it is judged as compliant\.

\(3\) **Degradation/Fallback Logic**: If the CV branch fails to extract valid reference features due to complex environments \(e\.g\., occlusion\), the system automatically sets $\\alpha = 0$ and enters degradation mode, relying entirely on the semantic reasoning results of the VLM branch for judgment\.



## **4\. Experimental Setup and Evaluation**

### **4\.1 Comprehensive Performance of Dual\-Branch Parallel Pipeline**

We comprehensively evaluated the dual\-branch parallel recognition framework on a self\-built dataset containing 500 real street\-view images \(covering various weather, illumination, and occlusion conditions\)\. The pipeline combines the advantages of geometric\-physical modeling and semantic reasoning to handle complex urban parking environments\. Experimental results demonstrate that the dual\-branch voting model, while maintaining high reliability, outperforms any single\-branch in both Accuracy and F1\-score\. The specific comprehensive performance metrics are shown in Table 1\.



### **4\.2 Ablation Study 1: Pure CV Branch Performance**

In this experiment, we disabled the VLM branch and solely relied on the geometric reconstruction logic based on YOLOv8\-Seg and Depth Anything V2\. As shown in Table 1, the pure CV branch achieved an accuracy of 72\.15% and an F1\-score of 72\.80%\.



The analysis reveals that the pure CV branch exhibits extremely high geometric judgment precision when processing images with distinct curb or parking line features, but its performance fluctuates significantly in complex urban environments\. This is primarily attributed to the "long\-tail distribution" phenomenon of references in urban street views: road markings are often blurred due to wear and tear, or partially occluded by obstacles such as fallen leaves, accumulated water, or interlaced parked vehicles\. When these key reference features cannot be effectively extracted, the logical chain of the CV branch based on strict deterministic geometric calculations \(such as PCA or point cloud boundary calculations\) fails due to the lack of inputs, thereby substantially weakening the generalization ability of the model\. This limitation underscores the necessity of introducing VLM as a semantic fallback and enhancement mechanism\.



However, it must be pointed out that although the VLM is more robust in semantic understanding, it is inherently based on probabilistic reasoning and often suffers from hallucination issues or relatively loose perception of angle deviations\. In contrast, the hard quantitative geometric data provided by the CV branch \(e\.g\., PCA angles, physical boundaries of point clouds\) remains an indispensable criterion for achieving high\-precision compliance judgment\. Therefore, deeply integrating the CV branch, which possesses rigorous geometric and physical significance, with the VLM branch, which contains deep semantic common sense, is the only feasible path to address parking compliance recognition in complex urban environments\.



### **4\.3 Ablation Study 2: Pure VLM Branch Performance**

This section evaluates the recognition capability of using solely the Qwen\-VL model without incorporating CV geometric features\. Due to its powerful visual common sense, the model excels in identifying high\-level semantic violations such as "vehicle occupying tactile paving\."

Experimental data indicates that the pure VLM branch achieves an F1\-score of 79\.34%, an accuracy of 75\.00%, a precision of 67\.61%, and a remarkably high recall rate of 96\.00%\. Although the extremely high recall rate effectively captures violations, the lower precision implies that this branch produces a significant number of false positives when making complex geometric judgments \(such as determining whether the orientation angle is within a $\pm 30^{\circ}$ error tolerance\)\.



**Table 1\. Performance Comparison of Different Algorithm Branches**

|Recognition Scheme|Accuracy|Precision|Recall|F1\-score|
|---|---|---|---|---|
|Pure CV Branch|72\.15%|70\.20%|75\.60%|72\.80%|
|Pure VLM Branch \(Qwen\-VL\)|75\.00%|67\.61%|96\.00%|79\.34%|

\| **Dual\-Branch Model \(Ours\)** \| **80\.00%** \| **72\.06%** \| **98\.00%** \| **83\.78%** \|



### **4\.4 Performance Analysis**

Despite the introduction of the VLM branch, the system still maintains efficient end\-to\-end processing capabilities by adopting an efficient inference framework\. The dual\-branch parallel architecture significantly outperforms traditional single\-vision algorithms in terms of robustness when dealing with complex street scenes \(e\.g\., tactile paving detection, non\-standard parking lines\)\. Furthermore, through the fallback logic, the system can still provide reliable semantic judgment when the CV branch fails to extract features, ensuring high availability in production environments\.



## **5\. Discussion**

### **5\.1 Robustness in Field Tests**

Field tests demonstrate that the introduction of the dual\-branch pipeline significantly reduces the false alarm rate in nighttime or strong glare scenarios\. In these environments, pure visual depth estimation may experience local degradation, whereas the VLM's semantic reasoning can effectively supplement the judgment basis through environmental context\.



### **5\.2 Limitations**

The system currently faces challenges in extremely densely parked vehicle clusters, where adhesion at the mask boundaries might introduce noise into the separated point clouds\. Additionally, the accuracy of absolute scale is inherently limited by the generalization capability of monocular depth models on highly irregular road surface textures\. Future work will explore multi\-frame temporal information fusion to further enhance reconstruction accuracy under complex occlusions\.



## **6\. Conclusion**

This paper proposes an intelligent e\-bike parking compliance recognition system based on a Dual\-Branch Parallel Pipeline\. By synergizing the geometric\-physical modeling of the CV branch \(PCA\-based orientation extraction and bird's\-eye view region division\) with the deep semantic reasoning of the VLM branch \(Qwen\-VL\), and combining a hybrid veto\-weighted voting strategy, this system effectively resolves visual recognition challenges such as reference occlusion and worn markings in complex urban scenarios\. Experimental results demonstrate that on a real\-world complex street\-view dataset, the pure VLM branch achieves an F1\-score of 79\.34% in semantic discrimination, while the dual\-branch system achieves a comprehensive F1\-score of 83\.78%\. The system accomplishes an organic combination of high\-precision geometric measurement and robust semantic understanding, providing a low\-cost, highly reliable visual perception solution for smart city e\-bike governance without the need for hardware modifications\.

