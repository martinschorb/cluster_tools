import os
import json
from datetime import datetime

import numpy as np
import luigi
# NOTE we don't need to bother with the file reader
# wrapper here, because paintera needs n5 files anyway.
import z5py

from ..import downscaling as sampling_tasks
from ..cluster_tasks import WorkflowBase
from ..downscaling import DownscalingWorkflow
# TODO
# from ..label_multisets import

from . import unique_block_labels as unique_tasks
from . import label_block_mapping as labels_to_block_tasks


class WritePainteraMetadata(luigi.Task):
    tmp_folder = luigi.Parameter()
    path = luigi.Parameter()
    label_group = luigi.Parameter()
    scale_factors = luigi.Parameter()
    original_scale = luigi.IntParameter()
    is_label_multiset = luigi.BoolParameter()
    resolution = luigi.ListParameter()
    offset = luigi.ListParameter()
    max_id = luigi.IntParameter()
    dependency = luigi.TaskParameter()

    def _write_log(self, msg):
        log_file = self.output().path
        with open(log_file, 'a') as f:
            f.write('%s: %s\n' % (str(datetime.now()), msg))

    def requires(self):
        return self.dependency

    def _write_downsampling_factors(self, group):
        # write the scale factors
        for scale, scale_factor in enumerate(self.scale_factors):
            ds = group['s%i' % scale]
            # we need to reverse the scale factors because paintera has axis order
            # XYZ and we have axis order ZYX
            ds.attrs['downsamplingFactors'] = scale_factor[::-1]

    def run(self):
        with z5py.File(self.path) as f:
            # write metadata for the top-level label group
            label_group = f[self.label_group]
            label_group.attrs['paintera_data'] = {'type': 'label'}
            label_group.attrs['maxId'] = self.max_id
            # write metadata for the label-data group
            data_group = f[os.path.join(self.label_group, 'data')]
            data_group.attrs['maxId'] = self.max_id
            data_group.attrs['multiScale'] = True
            data_group.attrs['offset'] = self.offset
            data_group.attrs['resolution'] = self.resolution
            data_group.attrs['isLabelMultiset'] = self.is_label_multiset
            self._write_downsampling_factors(data_group)
            # write metadata for block to label mapping
            scale_ds_pattern = os.path.join(self.labels_out_path, 'label-to-block-mapping', 's%%d')
            data_group.attrs["labelBlockLookup"] = {"type": "n5-filesystem",
                                                    "root": self.path,
                                                    "scaleDatasetPattern": scale_ds_pattern}
            # add metadata for unique labels group
            unique_group = f[os.path.join(self.label_group, 'unique-labels')]
            unique_group.attrs['multiScale'] = True
            self._write_downsampling_factors(unique_group)
        self._write_log('write metadata successfull')

    def output(self):
        return luigi.LocalTarget(os.path.join(self.tmp_folder,
                                              'write_paintera_metadata.log'))


class ConversionWorkflow(WorkflowBase):
    path = luigi.Parameter()
    raw_key = luigi.Parameter()
    label_in_key = luigi.Parameter()
    label_out_key = luigi.Parameter()
    label_scale = luigi.IntParameter()
    assignment_key = luigi.Parameter(default='')
    use_label_multiset = luigi.BoolParameter(default=False)
    offset = luigi.ListParameter(default=[0, 0, 0])
    resolution = luigi.ListParameter(default=[1, 1, 1])

    #####################################
    # Step 1 Implementations: make_labels
    #####################################

    def _link_labels(self, data_path, dependency):
        norm_path = os.path.abspath(os.path.realpath(self.path))
        src = os.path.join(norm_path, self.label_in_key)
        dst = os.path.join(data_path, 's%i' % self.label_scale)
        os.symlink(src, dst)
        return dependency

    # TODO implement
    def _make_label_multiset(self):
        raise NotImplementedError("Label multi-set not implemented yet")

    def _make_labels(self, dependency):

        # check if we have output labels already
        dst_key = os.path.join(self.label_out_key, 'data', 's%i' % self.label_scale)
        with z5py.File(self.path) as f:
            if dst_key in f:
                return dependency

        # we make the label output group
        with z5py.File(self.path) as f:
            g = f.require_group(self.label_out_key)
            dgroup = g.require_group('data')
            # resolve relative paths and links
            data_path = os.path.abspath(os.path.realpath(dgroup.path))

        # if we use label-multisets, we need to create the label multiset for this scale
        # otherwise, we just make a symlink
        # make symlink from input dataset to output dataset
        return self._make_label_multiset(dependency) if self.use_label_multiset\
            else self._link_labels(data_path, dependency)

    ######################################
    # Step 2 Implementations: align scales
    ######################################

    # TODO implement for label-multi-set
    def _upsample_labels(self, upsample_scales, scale_factors, dependency):
        task = getattr(sampling_tasks, self._get_task_name('Upscaling'))
        # reverse the scales for upsampling
        target_scales = upsample_scales[::-1]

        # run upsampling
        in_scale = self.label_scale
        in_key = os.path.join(self.label_out_key, 'data', 's%i' % in_scale)
        dep = dependency
        for out_scale in target_scales:
            out_key = os.path.join(self.label_out_key, 'data', 's%i' % out_scale)

            # find the relative scale factor
            scale_factor = [sf_out // sf_in for sf_out, sf_in
                            in zip(scale_factors[out_scale], scale_factors[in_scale])]
            dep = task(tmp_folder=self.tmp_folder, max_jobs=self.max_jobs,
                       config_dir=self.config_dir,
                       input_path=self.path, input_key=in_key,
                       output_path=self.path, output_key=out_key,
                       scale_factor=scale_factor, scale_prefix='s%i' % out_scale,
                       dependency=dep)

            in_scale = out_scale
            in_key = out_key
        return dep

    # TODO implement for label-multi-set
    def _downsample_labels(self, downsample_scales, scale_factors, dependency):
        task = getattr(sampling_tasks, self._get_task_name('Downscaling'))

        # run upsampling
        in_scale = self.label_scale
        in_key = os.path.join(self.label_out_key, 'data', 's%i' % in_scale)
        dep = dependency
        for out_scale in downsample_scales:
            out_key = os.path.join(self.label_out_key, 'data', 's%i' % out_scale)

            # find the relative scale factor
            scale_factor = [int(sf_out // sf_in) for sf_out, sf_in
                            in zip(scale_factors[out_scale], scale_factors[in_scale])]
            dep = task(tmp_folder=self.tmp_folder, max_jobs=self.max_jobs,
                       config_dir=self.config_dir,
                       input_path=self.path, input_key=in_key,
                       output_path=self.path, output_key=out_key,
                       scale_factor=scale_factor, scale_prefix='s%i' % out_scale,
                       dependency=dep)

            in_scale = out_scale
            in_key = out_key
        return dep

    def _align_scales(self, dependency):
        # check which sales we have in the raw data
        raw_dir = os.path.join(self.path, self.raw_key)
        raw_scales = os.listdir(raw_dir)
        raw_scales = [rscale for rscale in raw_scales
                      if os.path.isdir(os.path.join(raw_dir, rscale))]

        def isint(inp):
            try:
                int(inp)
                return True
            except ValueError:
                return False

        raw_scales = np.array([int(rscale[1:]) for rscale in raw_scales if isint(rscale[1:])])
        raw_scales = np.sort(raw_scales)

        # match the label scale and determine which scales we have to compute
        # via up - and downsampling
        scale_idx = np.argwhere(raw_scales == self.label_scale)[0][0]
        upsample_scales = raw_scales[:scale_idx]
        downsample_scales = raw_scales[scale_idx+1:]

        # load the scale factors from the raw dataset
        scale_factors = []
        with z5py.File(self.path) as f:
            for scale in raw_scales:
                scale_key = os.path.join(self.raw_key, 's%i' % scale)
                # we need to reverse the scale factors because paintera has axis order
                # XYZ and we have axis order ZYX
                if scale == 0:
                    scale_factors.append([1., 1., 1.])
                else:
                    scale_factors.append(f[scale_key].attrs['downsamplingFactors'][::-1])

        # upsample segmentations
        t_up = self._upsample_labels(upsample_scales, scale_factors, dependency)
        # downsample segmentations
        t_down = self._downsample_labels(downsample_scales, scale_factors, t_up)
        return t_down, scale_factors

    ############################################
    # Step 3 Implementations: make block uniques
    ############################################

    def _uniques_in_blocks(self, dependency, n_scales):
        task = getattr(unique_tasks, self._get_task_name('UniqueBlockLabels'))
        # require the unique-labels group
        with z5py.File(self.path) as f:
            f.require_group(os.path.join(self.label_out_key, 'unique-labels'))
        dep = dependency
        for scale in range(n_scales):
            in_key = os.path.join(self.label_out_key, 'data', 's%i' % scale)
            out_key = os.path.join(self.label_out_key, 'unique-labels', 's%i' % scale)
            dep = task(tmp_folder=self.tmp_folder, max_jobs=self.max_jobs,
                       config_dir=self.config_dir,
                       input_path=self.path, output_path=self.path,
                       input_key=in_key, output_key=out_key,
                       dependency=dep)
        return dep

    ##############################################
    # Step 4 Implementations: invert block uniques
    ##############################################

    def _label_block_mapping(self, dependency, n_scales):
        task = getattr(labels_to_block_tasks, self._get_task_name('LabelBlockMapping'))
        # require the labels-to-blocks group
        with z5py.File(self.path) as f:
            f.require_group(os.path.join(self.label_out_key, 'label-to-block-mapping'))
        # get the framgent max id
        with z5py.File(self.path) as f:
            max_id = f[self.label_in_key].attrs['maxId']
        # compte the label to block mapping for all scales
        dep = dependency
        for scale in range(n_scales):
            in_key = os.path.join(self.label_out_key, 'unique-labels', 's%i' % scale)
            out_key = os.path.join(self.label_out_key, 'label-to-block-mapping', 's%i' % scale)
            dep = task(tmp_folder=self.tmp_folder, max_jobs=self.max_jobs,
                       config_dir=self.config_dir,
                       input_path=self.path, output_path=self.path,
                       input_key=in_key, output_key=out_key,
                       number_of_labels=max_id + 1, dependency=dep)
        return dep

    #####################################################
    # Step 5 Implementations: fragment segment assignment
    #####################################################

    def _fragment_segment_assignment(self, dependency):
        if self.assignment_key == '':
            # get the framgent max id
            with z5py.File(self.path) as f:
                max_id = f[self.label_in_key].attrs['maxId']
            return dependency, max_id
        else:
            # TODO should make this a task
            with z5py.File(self.path) as f:
                assignments = f[self.assignment_key][:]
                n_fragments = len(assignments)

                # find the fragments which have non-trivial assignment
                segment_ids, counts = np.unique(assignments, return_counts=True)
                fragment_ids = np.arange(n_fragments, dtype='uint64')
                fragment_ids_to_counts = counts[segment_ids[fragment_ids]]
                non_triv_fragments = fragment_ids[fragment_ids_to_counts > 1]
                non_triv_segments = assignments[non_triv_fragment_ids]
                non_triv_segments += n_fragments

                # determine the overall max id
                max_id = int(non_triv_segments).max()

                # TODO do we need to assign a special value to ignore label (0) ?
                frag_to_seg = np.vstack((non_triv_fragments, non_triv_segments))

                out_key = os.path.join(self.label_out_key, 'fragment-segment-assignment')
                chunks = (1, n_fragments)
                f.require_dataset(out_key, data=frag_to_seg, compression='gzip', chunks=chunks)
            return dependency, max_id

    def requires(self):
        # first, we make the labels at label_out_key
        # (as label-multi-set if specified)
        t1 = self._make_labels(self.dependency)
        # next, align the scales of labels and raw data
        t2, scale_factors = self._align_scales(t1)
        # # next, compute the mapping of unique labels to blocks
        t3 = self._uniques_in_blocks(t2, len(scale_factors))
        # # next, compute the inverse mapping
        t4 = self._label_block_mapping(t3, len(scale_factors))
        # # next, compute the fragment-segment-assignment
        t5, max_id = self._fragment_segment_assignment(t4)
        # finally, write metadata
        t6 = WritePainteraMetadata(tmp_folder=self.tmp_folder, path=self.path,
                                   label_group=self.label_out_key, scale_factors=scale_factors,
                                   original_scale=self.label_scale,
                                   is_label_multiset=self.use_label_multiset,
                                   resolution=self.resolution, offset=self.offset,
                                   max_id=max_id, dependency=t5)
        return t6

    @staticmethod
    def get_config():
        configs = super(ConversionWorkflow, ConversionWorkflow).get_config()
        configs.update({'unique_block_labels': unique_tasks.UniqueBlockLabelsLocal.default_task_config(),
                        'downscaling': sampling_tasks.DownscalingLocal.default_task_config(),
                        'upscaling': sampling_tasks.UpscalingLocal.default_task_config()})
        return configs