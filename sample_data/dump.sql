-- Hydra sample mysqldump
-- Base : dd_assets_tracking

CREATE TABLE `assets` (
  `id` int NOT NULL,
  `project` varchar(64), `entity_name` varchar(128),
  `task_name` varchar(64), `av_name` varchar(64),
  `node_name` varchar(128), `version` int,
  `created_by` varchar(64)
);

INSERT INTO `assets` (`id`, `project`, `entity_name`, `task_name`, `av_name`, `node_name`, `version`, `created_by`) VALUES
(1,'qua','077_02000','modeling','','chaise',5,'pipeline'),
(2,'qua','077_02000','modeling','','table',5,'pipeline'),
(3,'qua','077_02000','modeling','','chaise',7,'pipeline'),
(4,'qua','077_02000','modeling','','table',7,'pipeline'),
(5,'qua','077_02000','rigging','','chaise_rig',3,'pipeline'),
(6,'qua','077_02000','shading','','chaise_shd',2,'pipeline'),
(7,'qua','077_02000','shading','','table_shd',2,'pipeline'),
(8,'qua','077_02000','tracking','','camera_track',6,'pipeline'),
(9,'qua','077_02000','layout','','layout_cache',8,'pipeline'),
(10,'qua','077_02000','animation','','chaise_anim',12,'pipeline'),
(11,'qua','077_02000','animation','','quasimodo',12,'pipeline'),
(12,'qua','077_02000','animation','','quasimodo',14,'pipeline'),
(13,'qua','077_02000','lighting','','render',4,'pipeline'),
(14,'qua','077_02000','lighting','','deep',4,'pipeline'),
(15,'qua','077_02000','lighting','','camera_layer_01_camera_abc',4,'pipeline');

CREATE TABLE `scenes` (
  `id` int NOT NULL,
  `name` varchar(255), `project` varchar(64),
  `entity_name` varchar(128), `task_name` varchar(64),
  `av_name` varchar(64), `version` int
);

INSERT INTO `scenes` (`id`, `name`, `project`, `entity_name`, `task_name`, `av_name`, `version`) VALUES
(100,'qua_077_02000_modeling_toto_v005','qua','077_02000','modeling','',5),
(101,'qua_077_02000_modeling_toto_v007','qua','077_02000','modeling','',7),
(110,'qua_077_02000_rigging_ana_v003','qua','077_02000','rigging','',3),
(120,'qua_077_02000_shading_bob_v002','qua','077_02000','shading','',2),
(130,'qua_077_02000_tracking_lea_v006','qua','077_02000','tracking','',6),
(140,'qua_077_02000_layout_max_v008','qua','077_02000','layout','',8),
(150,'qua_077_02000_animation_max_v012','qua','077_02000','animation','',12),
(151,'qua_077_02000_animation_max_v014','qua','077_02000','animation','',14),
(160,'qua_077_02000_lighting_kim_v004','qua','077_02000','lighting','',4),
(170,'qua_077_02000_compositing_sam_v019','qua','077_02000','compositing','',19);

CREATE TABLE `binds` (
  `asset_id` int, `scene_id` int, `active` tinyint
);

INSERT INTO `binds` (`asset_id`, `scene_id`, `active`) VALUES
(13,170,1),
(14,170,1),
(15,170,1),
(10,160,1),
(11,160,1),
(6,160,1),
(7,160,1),
(9,160,1),
(5,150,1),
(9,150,1),
(1,110,1),
(1,120,1),
(2,120,1),
(8,140,1),
(2,140,1),
(3,170,0);

INSERT INTO `assets_parents` (`a`, `b`) VALUES (1,2),(3,4);
